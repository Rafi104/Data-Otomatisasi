"""Simulasi FireGuard (satu file, mandiri): jalankan  python simulasi.py --runs 5

Berisi logika keputusan voting 2 dari 3 (sama dengan firmware ESP32) + skenario uji.
Hasil ini simulasi pengembangan, BUKAN hasil KPI prototipe fisik.
"""
import argparse
import random



try:
    from time import ticks_diff as _diff
except ImportError:  # CPython
    def _diff(a, b):
        return a - b

STANDBY = "STANDBY"
VERIFY = "VERIFY"
PUMPING = "PUMPING"
NO_WATER = "NO_WATER"
CLEARING = "CLEARING"

GLITCH_MS = 1000  # kriteria boleh putus sesaat selama verifikasi tanpa mengulang hitungan


class FireLogic:
    def __init__(self, smoke_on=300, smoke_off=250, temp_abs_c=45.0, temp_delta_c=8.0,
                 temp_hyst_c=2.0, ambient_alpha=0.002, verify_ms=5000,
                 pump_off_delay_ms=3000, alarm_off_delay_ms=6000,
                 water_warn_pct=20, water_min_pct=10, water_hyst_pct=5,
                 max_pump_ms=60000):
        self.smoke_on = smoke_on
        self.smoke_off = smoke_off
        self.temp_abs_c = temp_abs_c
        self.temp_delta_c = temp_delta_c
        self.temp_hyst_c = temp_hyst_c
        self.ambient_alpha = ambient_alpha
        self.verify_ms = verify_ms
        self.pump_off_delay_ms = pump_off_delay_ms
        self.alarm_off_delay_ms = alarm_off_delay_ms
        self.water_warn_pct = water_warn_pct
        self.water_min_pct = water_min_pct
        self.water_hyst_pct = water_hyst_pct
        self.max_pump_ms = max_pump_ms

        self.state = STANDBY
        self.alarm = False
        self.pump = False
        self.ambient = None
        self.response_ms = None
        self._smoke_ind = False
        self._temp_ind = False
        self._low_warn = False
        self._t_detect = None
        self._verify_since = None
        self._low_since = None
        self._pump_start = None

    # ---- indikator ----
    def _indicators(self, flame1, flame2, smoke_ppm, temp_c):
        flame = bool(flame1 or flame2)
        if smoke_ppm is not None:
            if self._smoke_ind:
                self._smoke_ind = smoke_ppm > self.smoke_off
            else:
                self._smoke_ind = smoke_ppm >= self.smoke_on
        else:
            self._smoke_ind = False
        if temp_c is not None:
            if self.ambient is None:
                self.ambient = temp_c
            h = self.temp_hyst_c
            over = temp_c >= self.temp_abs_c or (temp_c - self.ambient) >= self.temp_delta_c
            under = temp_c < (self.temp_abs_c - h) and (temp_c - self.ambient) < (self.temp_delta_c - h)
            self._temp_ind = (not under) if self._temp_ind else over
        else:
            self._temp_ind = False
        return flame, self._smoke_ind, self._temp_ind

    # ---- satu siklus ----
    def update(self, now_ms, flame1, flame2, smoke_ppm, temp_c, water_pct):
        events = []
        f, s, t = self._indicators(flame1, flame2, smoke_ppm, temp_c)
        votes = int(f) + int(s) + int(t)

        low_warn = water_pct is not None and water_pct <= self.water_warn_pct
        if low_warn and not self._low_warn:
            events.append(("low_water_warning", "level=%s%%" % water_pct))
        self._low_warn = low_warn

        if votes >= 2:
            self._low_since = None
        elif self._low_since is None:
            self._low_since = now_ms
        low_for = 0 if self._low_since is None else _diff(now_ms, self._low_since)

        st = self.state
        if st == STANDBY:
            if votes >= 2:
                self.state = VERIFY
                self.alarm = True
                self.response_ms = None
                self._t_detect = now_ms
                self._verify_since = now_ms
                events.append(("alarm_on", "votes=%d api=%d asap=%d suhu=%d" % (votes, f, s, t)))
            elif votes == 0 and temp_c is not None and self.ambient is not None:
                self.ambient += self.ambient_alpha * (temp_c - self.ambient)

        elif st == VERIFY:
            if votes >= 2:
                if self._verify_since is None:
                    self._verify_since = now_ms
                if _diff(now_ms, self._verify_since) >= self.verify_ms:
                    self._start_pump(now_ms, water_pct, events)
            else:
                if low_for >= GLITCH_MS:
                    self._verify_since = None
                if low_for >= self.alarm_off_delay_ms:
                    self._to_standby(events, "alarm_cancelled")

        elif st == PUMPING:
            if water_pct is not None and water_pct <= self.water_min_pct:
                self.pump = False
                self.state = NO_WATER
                events.append(("pump_stop_low_water", "level=%s%%" % water_pct))
            elif _diff(now_ms, self._pump_start) >= self.max_pump_ms:
                self.pump = False
                self.state = CLEARING
                events.append(("pump_timeout", "max=%dms" % self.max_pump_ms))
            elif low_for >= self.pump_off_delay_ms:
                self.pump = False
                self.state = CLEARING
                events.append(("pump_off", "aman %dms" % low_for))

        elif st == NO_WATER:
            if votes >= 2:
                if water_pct is not None and water_pct > self.water_min_pct + self.water_hyst_pct:
                    self._start_pump(now_ms, water_pct, events)
            elif low_for >= self.alarm_off_delay_ms:
                self._to_standby(events, "alarm_off")

        elif st == CLEARING:
            if votes >= 2:
                self.state = VERIFY
                self._verify_since = now_ms
                events.append(("re_trigger", "kriteria terpenuhi lagi"))
            elif low_for >= self.alarm_off_delay_ms:
                self._to_standby(events, "alarm_off")

        verify_left = None
        if self.state == VERIFY and self._verify_since is not None:
            verify_left = max(0, self.verify_ms - _diff(now_ms, self._verify_since))

        return {
            "state": self.state, "alarm": self.alarm, "pump": self.pump, "votes": votes,
            "ind_flame": f, "ind_smoke": s, "ind_temp": t, "low_water_warn": low_warn,
            "response_ms": self.response_ms, "verify_left_ms": verify_left,
            "ambient_c": self.ambient, "events": events,
        }

    def _start_pump(self, now_ms, water_pct, events):
        if water_pct is None or water_pct > self.water_min_pct:
            self.pump = True
            self.state = PUMPING
            self._pump_start = now_ms
            if self.response_ms is None and self._t_detect is not None:
                self.response_ms = _diff(now_ms, self._t_detect)
            events.append(("pump_on", "respons=%sms" % self.response_ms))
        elif self.state != NO_WATER:
            self.state = NO_WATER
            events.append(("pump_blocked_low_water", "level=%s%%" % water_pct))

    def _to_standby(self, events, name):
        self.state = STANDBY
        self.alarm = False
        self.pump = False
        self._verify_since = None
        events.append((name, ""))


DT = 200  # ms


def run_scenario(name, rng, duration_s=60, ignite_s=5, water0=100.0):
    logic = FireLogic()
    fire = 0.0
    smoke, temp, water = 120.0, 29.0, water0
    first_alarm = first_pump = None
    final = None
    for k in range(int(duration_s * 1000 / DT)):
        t_ms, t_s = k * DT, k * DT / 1000.0
        dt = DT / 1000.0
        burning = name in ("api", "api_air_rendah") and t_s >= ignite_s
        if logic.pump:
            fire = max(0.0, fire - dt * 0.12)
            water = max(0.0, water - dt * 0.45)
        elif burning:
            fire = min(1.0, fire + dt * 0.15)

        tgt_smoke, tgt_temp, f1p, f2p = 120 + fire * 650, 29 + fire * 32, 0.0, 0.0
        if fire > 0.25:
            f1p = f2p = 0.95
        if name == "asap_masak" and t_s >= ignite_s:
            tgt_smoke, tgt_temp = 450, 33
        if name == "sinar_matahari" and t_s >= ignite_s:
            f1p = 0.5
        if name == "hari_panas" and t_s >= ignite_s:
            tgt_temp = 44
        smoke += (tgt_smoke - smoke) * dt * 0.35
        temp += (tgt_temp - temp) * dt * 0.08

        out = logic.update(t_ms, rng.random() < f1p, rng.random() < f2p,
                           smoke + rng.gauss(0, 12), temp + rng.gauss(0, 0.15),
                           water + rng.gauss(0, 0.3))
        if out["alarm"] and first_alarm is None:
            first_alarm = t_s
        if out["pump"] and first_pump is None:
            first_pump = t_s
        final = out
    return {"skenario": name, "alarm_s": first_alarm, "pompa_s": first_pump,
            "respons_s": None if final["response_ms"] is None else final["response_ms"] / 1000.0,
            "state_akhir": final["state"], "air_akhir_pct": round(water, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    names = ["api", "tanpa_api", "asap_masak", "sinar_matahari", "hari_panas", "api_air_rendah"]
    print("%-16s %-4s %-9s %-9s %-10s %-9s" % ("skenario", "run", "alarm(s)", "pompa(s)", "respons(s)", "state"))
    for n in names:
        for i in range(args.runs):
            r = run_scenario(n, rng, water0=8.0 if n == "api_air_rendah" else 100.0)
            fmt = lambda v: "-" if v is None else "%.1f" % v
            print("%-16s %-4d %-9s %-9s %-10s %-9s" % (n, i + 1, fmt(r["alarm_s"]), fmt(r["pompa_s"]),
                                                      fmt(r["respons_s"]), r["state_akhir"]))


if __name__ == "__main__":
    main()
