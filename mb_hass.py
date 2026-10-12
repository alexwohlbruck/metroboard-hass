# mb_hass.py -- Home Assistant (MQTT) bridge for the Design Rules Metroboard
#
# Runs as a task inside the Metroboard app's asyncio loop, started by a hook in
# the app's main(): `import mb_hass; asyncio.create_task(mb_hass.run(ctx))`.
#
# Uses the asynchronous mqtt_as client, so no network call can block the event
# loop. The app owns Wi-Fi; this module never connects or disconnects it.
# Everything is wrapped so a failure here can't affect the display.
#
# Lives at the root of the board's filesystem, which vendor updates don't touch.

import asyncio
import ujson as json
import time
import logging

try:
    import utils
except Exception:
    utils = None

log = logging.getLogger("MBHASS")
CONFIG_PATH = "/mb_hass.json"

BRIGHTNESS_OPTS = ["LOW", "MED", "HIGH"]
COLOR_OPTS      = ["COLOR", "WHITE"]
TRANSIT_OPTS    = ["SOLID", "PULSED"]
FREQ_OPTS       = ["FASTEST", "10", "30"]
EFFECT_OPTS     = ["Line Colors", "White"]
_BR_TO_255      = {"LOW": 85, "MED": 170, "HIGH": 255}


def _brightness_to_level(b):
    try:
        b = int(b)
    except Exception:
        return "HIGH"
    if b <= 85:
        return "LOW"
    if b <= 170:
        return "MED"
    return "HIGH"


def _load_cfg():
    cfg = {
        "broker": "homeassistant.local",
        "port": 1883,
        "user": None,
        "password": None,
        "discovery_prefix": "homeassistant",
        "keepalive": 60,
        "state_interval_s": 30,
        "debug": False,
    }
    try:
        with open(CONFIG_PATH) as f:
            u = json.load(f)
        if isinstance(u, dict):
            cfg.update(u)
    except Exception:
        pass
    return cfg


class Bridge:
    def __init__(self, ctx, cfg, client):
        self.ctx = ctx
        self.cfg = cfg
        self.client = client
        from boardinfo import BoardInfo
        self.device_id = (BoardInfo.get_device_id() or "mb-unknown").lower()
        self.variant = BoardInfo.get_variant() or "unknown"
        self.fw = BoardInfo.get_firmware_version() or "unknown"
        self.base = cfg.get("base") or ("metroboard/" + self.device_id)
        self.t_avail = self.base + "/availability"
        self.t_state = self.base + "/state"
        self.t_lstate = self.base + "/light/state"
        self.t_set = self.base + "/set/"
        self._nm_task = None
        self.state = self._read_initial_state()

    # -------- state helpers --------
    def _read_initial_state(self):
        ctx = self.ctx
        st = {"power": "ON", "brightness": "HIGH", "color_mode": "COLOR",
              "transit_mode": "SOLID", "update_freq": "FASTEST", "night_mode": "OFF"}
        try:
            if utils:
                stored = utils.get_stored_settings() or {}
                for k in ("brightness", "color_mode", "transit_mode", "update_freq"):
                    if stored.get(k):
                        st[k] = stored[k]
        except Exception:
            pass
        try:
            if getattr(ctx, "realtime", None) is not None:
                st["power"] = "ON" if ctx.realtime.is_running() else "OFF"
                st["transit_mode"] = ctx.realtime.get_transit_mode()
        except Exception:
            pass
        try:
            st["night_mode"] = "ON" if (getattr(ctx, "nightmode", None) is not None) else "OFF"
        except Exception:
            pass
        return st

    def _net_info(self):
        ip, rssi = None, None
        try:
            import network
            w = network.WLAN(network.STA_IF)
            if w.active():
                ip = w.ifconfig()[0]
                try:
                    rssi = w.status("rssi")
                except Exception:
                    rssi = None
        except Exception:
            pass
        return ip, rssi

    def _state_payload(self):
        ip, rssi = self._net_info()
        try:
            avg = int(getattr(self.ctx, "avg_resp_time_ms", 0)) or None
        except Exception:
            avg = None
        p = dict(self.state)
        p["fw"] = self.fw
        p["variant"] = self.variant
        p["ip"] = ip
        p["rssi"] = rssi
        p["avg_ms"] = avg
        return json.dumps(p)

    def _light_state_payload(self):
        eff = "White" if self.state.get("color_mode") == "WHITE" else "Line Colors"
        return json.dumps({"state": self.state.get("power", "ON"),
                           "brightness": _BR_TO_255.get(self.state.get("brightness", "HIGH"), 255),
                           "effect": eff})

    # -------- publish helpers (async) --------
    async def _pub(self, topic, payload, retain=False):
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            await self.client.publish(topic, payload, retain, 0)
            return True
        except Exception as e:
            log.warning("publish %s failed: %s", topic, e)
            return False

    def _device_block(self):
        v = self.variant or "unknown"
        vcap = v[:1].upper() + v[1:]
        return {"identifiers": [self.device_id], "name": "Metroboard (%s)" % vcap,
                "manufacturer": "Design Rules", "model": "Metroboard %s" % vcap,
                "sw_version": self.fw}

    async def _disc(self, component, obj_id, cfg):
        prefix = self.cfg.get("discovery_prefix", "homeassistant")
        topic = "%s/%s/%s/%s/config" % (prefix, component, self.device_id, obj_id)
        base = {"availability_topic": self.t_avail, "device": self._device_block(),
                "unique_id": "%s_%s" % (self.device_id, obj_id),
                "object_id": "%s_%s" % (self.device_id, obj_id)}
        base.update(cfg)
        await self._pub(topic, json.dumps(base), retain=True)

    async def _select(self, obj_id, name, options, key):
        await self._disc("select", obj_id, {"name": name, "command_topic": self.t_set + obj_id,
            "state_topic": self.t_state, "value_template": "{{ value_json.%s }}" % key,
            "options": options, "icon": "mdi:tune-variant"})

    async def _switch(self, obj_id, name, key, icon):
        await self._disc("switch", obj_id, {"name": name, "command_topic": self.t_set + obj_id,
            "state_topic": self.t_state, "value_template": "{{ value_json.%s }}" % key,
            "payload_on": "ON", "payload_off": "OFF", "icon": icon})

    async def _sensor(self, obj_id, name, key, unit=None, dev_class=None):
        c = {"name": name, "state_topic": self.t_state,
             "value_template": "{{ value_json.%s }}" % key, "entity_category": "diagnostic"}
        if unit:
            c["unit_of_measurement"] = unit
        if dev_class:
            c["device_class"] = dev_class
        await self._disc("sensor", obj_id, c)

    async def _light(self):
        await self._disc("light", "light", {"schema": "json", "name": "Display",
            "command_topic": self.t_set + "light", "state_topic": self.t_lstate,
            "brightness": True, "brightness_scale": 255, "supported_color_modes": ["brightness"],
            "effect": True, "effect_list": EFFECT_OPTS, "icon": "mdi:subway-variant"})

    async def publish_discovery(self):
        await self._light()
        await self._select("brightness", "Brightness", BRIGHTNESS_OPTS, "brightness")
        await self._select("color_mode", "Color Mode", COLOR_OPTS, "color_mode")
        await self._select("transit_mode", "Transit Mode", TRANSIT_OPTS, "transit_mode")
        await self._select("update_freq", "Update Frequency", FREQ_OPTS, "update_freq")
        await self._switch("night_mode", "Night Mode", "night_mode", "mdi:weather-night")
        await self._sensor("avg_ms", "Avg Response", "avg_ms", unit="ms")
        await self._sensor("rssi", "Wi-Fi Signal", "rssi", unit="dBm", dev_class="signal_strength")
        await self._sensor("fw", "Firmware", "fw")
        await self._sensor("ip", "IP Address", "ip")

    async def publish_state(self):
        await self._pub(self.t_state, self._state_payload(), retain=True)
        await self._pub(self.t_lstate, self._light_state_payload(), retain=True)

    # -------- control (sync side effects on ctx) --------
    def _persist(self, **kw):
        try:
            if utils:
                utils.update_config(kw)
        except Exception:
            pass
        try:
            btns = getattr(self.ctx, "buttons", None)
            if btns is not None:
                for k, v in kw.items():
                    if k in btns.state:
                        btns.state[k] = v
        except Exception:
            pass

    def _set_night_mode(self, enable):
        ctx = self.ctx
        try:
            from nightmode import NightModeManager
        except Exception:
            return
        if enable:
            if getattr(ctx, "nightmode", None) is None:
                mgr = NightModeManager(ctx.lights, day_level=self.state.get("brightness", "HIGH"))
                ctx.nightmode = mgr
                self._nm_task = asyncio.create_task(mgr.run())
        else:
            if self._nm_task is not None:
                try:
                    self._nm_task.cancel()
                except Exception:
                    pass
                self._nm_task = None
            mgr = getattr(ctx, "nightmode", None)
            if mgr is not None:
                try:
                    mgr._exit()
                    ctx.lights.metro.reload_trains()
                    ctx.lights.write()
                except Exception:
                    pass
            ctx.nightmode = None

    def apply(self, entity, val):
        ctx = self.ctx
        if entity == "light":
            self._apply_light(val)
            return
        val = (val or "").strip()
        if entity == "brightness" and val in BRIGHTNESS_OPTS:
            try:
                ctx.lights.set_brightness(val)
                ctx.lights.metro.reload_trains()
                ctx.lights.write()
                if getattr(ctx, "nightmode", None):
                    ctx.nightmode.set_day_brightness(val)
            except Exception:
                pass
            self.state["brightness"] = val
            self._persist(brightness=val)
        elif entity == "color_mode" and val in COLOR_OPTS:
            try:
                ctx.lights.set_color_mode(val)
                ctx.lights.metro.reload_trains()
                ctx.lights.write()
            except Exception:
                pass
            self.state["color_mode"] = val
            self._persist(color_mode=val)
        elif entity == "transit_mode" and val in TRANSIT_OPTS:
            ctx.realtime.set_transit_mode(val)
            self.state["transit_mode"] = val
            self._persist(transit_mode=val)
        elif entity == "update_freq" and val in FREQ_OPTS:
            ctx.realtime.set_update_frequency(val)
            self.state["update_freq"] = val
            self._persist(update_freq=val)
        elif entity == "night_mode" and val in ("ON", "OFF"):
            self._set_night_mode(val == "ON")
            self.state["night_mode"] = val
            self._persist(night_mode=(val == "ON"))

    def _apply_light(self, raw):
        ctx = self.ctx
        try:
            cmd = json.loads(raw)
        except Exception:
            return
        state = cmd.get("state")
        if cmd.get("effect") is not None:
            cm = "WHITE" if str(cmd["effect"]).lower().startswith("white") else "COLOR"
            try:
                ctx.lights.set_color_mode(cm)
                ctx.lights.metro.reload_trains()
                ctx.lights.write()
            except Exception:
                pass
            self.state["color_mode"] = cm
            self._persist(color_mode=cm)
        if cmd.get("brightness") is not None:
            lvl = _brightness_to_level(cmd["brightness"])
            try:
                ctx.lights.set_brightness(lvl)
                ctx.lights.metro.reload_trains()
                ctx.lights.write()
                if getattr(ctx, "nightmode", None):
                    ctx.nightmode.set_day_brightness(lvl)
            except Exception:
                pass
            self.state["brightness"] = lvl
            self._persist(brightness=lvl)
            if state is None:
                state = "ON"
        if state == "OFF":
            try:
                ctx.realtime.pause()
            except Exception:
                pass
            try:
                ctx.lights.clear()
                ctx.lights.write()
            except Exception:
                pass
            self.state["power"] = "OFF"
        elif state == "ON":
            try:
                ctx.realtime.resume()
            except Exception:
                pass
            self.state["power"] = "ON"


# Subclass: the Metroboard app owns wifi; never (re)associate or disconnect it.
def _make_client(cfg):
    import mqtt_as
    from boardinfo import BoardInfo

    class _KeepWifi:
        # mqtt_as disconnects Wi-Fi whenever the MQTT link drops, then expects
        # to reconnect it. The app only connects Wi-Fi once at boot, so the
        # board would stay offline. Pass everything through except turning
        # Wi-Fi off; without an explicit disconnect, MicroPython reconnects on
        # its own after a real outage.
        def __init__(self, sta):
            self._sta = sta

        def disconnect(self):
            pass

        def active(self, *args):
            if args and not args[0]:
                return None
            return self._sta.active(*args)

        def __getattr__(self, name):
            return getattr(self._sta, name)

    class _AppMQTT(mqtt_as.MQTTClient):
        def __init__(self, config):
            super().__init__(config)
            self._sta_if = _KeepWifi(self._sta_if)

        async def wifi_connect(self, quick=False):
            # The app connects Wi-Fi at boot and never again. Give MicroPython's
            # own reconnect 30 s after a real outage, then reconnect ourselves.
            s = self._sta_if
            for i in range(120):
                if s.isconnected():
                    return
                if i == 30 and self._ssid:
                    try:
                        s.connect(self._ssid, self._wifi_pw)
                    except OSError:
                        pass
                await asyncio.sleep(1)
            raise OSError("wifi not up")

    ssid, pw = (None, None)
    try:
        ssid, pw = BoardInfo.get_wifi_credentials()
    except Exception:
        pass
    device_id = (BoardInfo.get_device_id() or "mb-unknown").lower()
    base = "metroboard/" + device_id

    mc = dict(mqtt_as.config)
    mc["client_id"] = device_id.encode()
    mc["server"] = cfg["broker"]
    mc["port"] = int(cfg.get("port", 1883))
    mc["user"] = cfg.get("user") or ""
    mc["password"] = cfg.get("password") or ""
    mc["keepalive"] = int(cfg.get("keepalive", 60))
    mc["ssid"] = ssid
    mc["wifi_pw"] = pw
    mc["queue_len"] = 10
    mc["will"] = (base + "/availability", b"offline", True, 0)
    return _AppMQTT(mc)


async def _messages(bridge):
    # Drain inbound command messages (non-blocking async iterator).
    async for topic, msg, retained in bridge.client.queue:
        try:
            t = topic.decode() if isinstance(topic, (bytes, bytearray)) else topic
            m = msg.decode() if isinstance(msg, (bytes, bytearray)) else msg
            if t.startswith(bridge.t_set):
                bridge.apply(t[len(bridge.t_set):], m)
                await bridge.publish_state()
        except Exception as e:
            log.warning("msg handler error: %s", e)


async def _on_up(bridge):
    # Runs on every (re)connection: re-subscribe, (re)publish discovery + state.
    client = bridge.client
    while True:
        await client.up.wait()
        client.up.clear()
        try:
            await client.subscribe(bridge.t_set + "#", 1)
            await bridge.publish_discovery()
            await bridge._pub(bridge.t_avail, b"online", retain=True)
            await bridge.publish_state()
            log.info("MQTT (re)connected and announced")
        except Exception as e:
            log.warning("on_up error: %s", e)


_started = False


async def run(ctx):
    # Only start once, even if more than one launcher calls this.
    global _started
    if _started:
        return
    _started = True
    try:
        import gc
        gc.collect()
        await asyncio.sleep(5)
        gc.collect()
        cfg = _load_cfg()
        if cfg.get("debug"):
            try:
                logging.Logger.set_enabled(True)
            except Exception:
                pass
        gc.collect()
        client = _make_client(cfg)
        gc.collect()
        bridge = Bridge(ctx, cfg, client)
        gc.collect()
        asyncio.create_task(_messages(bridge))
        asyncio.create_task(_on_up(bridge))
        log.info("Starting Metroboard HA bridge (mqtt_as) -> %s:%s", cfg["broker"], cfg["port"])
        # mqtt_as.connect() is async and non-blocking; it will keep retrying
        # internally after the first success. First connect may raise if the
        # broker is unreachable; retry gently without ever blocking the loop.
        while True:
            try:
                await client.connect()
                break
            except Exception as e:
                log.warning("initial MQTT connect failed: %s (retry 10s)", e)
                await asyncio.sleep(10)
        si = int(cfg.get("state_interval_s", 30))
        while True:
            await asyncio.sleep(si)
            if client.isconnected():
                try:
                    await bridge.publish_state()
                except Exception as e:
                    log.warning("periodic state failed: %s", e)
    except asyncio.CancelledError:
        return
    except Exception as e:
        try:
            log.warning("mb_hass.run exited: %s", e)
        except Exception:
            pass
