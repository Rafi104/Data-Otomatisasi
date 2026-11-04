#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
SISTEM SPRINKLER & PENDETEKSI KEBAKARAN OTOMATIS (FIREGUARD) BERBASIS ESP32 & IoT
TUGAS BESAR OTOMASI SISTEM - KELOMPOK 06 TEKNIK INDUSTRI UNS
Dosen Pengampu:
- Prof. Dr.Eng. Ir. Pringgo Widyo Laksono, S.T., M.Eng., IPU, ASEAN Eng
- Prof. Dr. Ir. Lobes Herdiman, M.T.

Aplikasi Simulasi Python Desktop (Tkinter) Standalone & 100% Zero-Dependency.
Cukup jalankan: python simulasi.py

Mode lain:
    python simulasi.py --cli                  # uji 6 skenario tanpa jendela (teks)
    python simulasi.py --cli --csv hasil.csv  # sekaligus simpan data waktu ke CSV
    python simulasi.py --web                  # buka versi maket 3D di browser

Semua angka diambil dari maket sprinkler 3D (fungsi step() di file HTML).
Hasil ini simulasi pengembangan, BUKAN hasil KPI prototipe fisik.

Perbaikan versi ini:
  1. Alarm/pompa kini tertutup sendiri setelah semua sensor normal (alarm palsu tidak
     lagi mengunci sistem).
  2. Sistem siaga lagi (re-arm) setelah penutupan, jadi api kedua tetap terdeteksi.
  3. Skenario 7 "Masak + panas" (asap + suhu tinggi tanpa api) menguji kelemahan voting.
  + Peringatan air tandon di bawah 20% dan pencarian file HTML 3D otomatis.
=============================================================================
"""

import sys
import os
import math
import time
import csv
import json
import pathlib
import random
import datetime
import webbrowser

try:
    import tkinter as tk
except ImportError:          # mode --cli tetap bisa jalan tanpa Tkinter
    tk = None


# =============================================================================
# 1. KONTROLER OTOMASI ESP32 (LOGIKA VOTING 2 DARI 3 SESUAI MAKET 3D)
# =============================================================================
SCENARIO_NAMES = {
    1: "Api sungguhan",
    2: "Tanpa api",
    3: "Asap masak",
    4: "Sinar matahari",
    5: "Hari panas",
    6: "Api, air rendah",
    7: "Masak + panas",
}


def find_html():
    """Cari file maket 3D (HTML) di folder yang sama dengan skrip ini."""
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("uji_skenario_3d.html", "index.html"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    for name in sorted(os.listdir(base)):
        low = name.lower()
        if low.endswith(".html") and ("maket" in low or "sprinkler" in low):
            return os.path.join(base, name)
    return None


class FireGuardController:
    def __init__(self):
        # Ambang batas indikator (sama dengan maket 3D)
        self.FLAME_ON = 0.35         # api dianggap terdeteksi sensor IR bila nyala > ini
        self.MQ_ON = 300.0           # ppm, ambang asap MQ-2
        self.TEMP_ON = 42.0          # derajat C, ambang suhu DHT22
        self.VOTES_NEEDED = 2        # voting minimal 2 dari 3 indikator

        # Waktu penutupan sistem setelah api padam
        self.PUMP_OFF_DELAY = 1.2    # detik
        self.ALARM_OFF_DELAY = 3.0   # detik

        # Dinamika api, sensor, dan air tandon
        self.FIRE_GROW = 0.45        # api membesar per detik
        self.FIRE_DROP = 0.40        # api mengecil per detik saat disiram
        self.MQ_GAIN, self.MQ_RATE = 650.0, 0.55
        self.T_GAIN, self.T_RATE = 32.0, 0.30
        self.WATER_DRAIN = 4.5       # % tandon per detik saat pompa jalan
        self.WATER_MIN = 5.0         # % pompa berhenti di level ini
        self.WATER_WARN = 20.0       # % peringatan air hampir habis

        self.scenario_id = None
        self.reset()

    def reset(self):
        # Parameter dinamis
        self.t = 0.0
        self.burning = False
        self.fire = 0.0              # 0.0 - 1.0
        self.mq_base = 120.0         # asap lingkungan (ppm)
        self.t_base = 29.0           # suhu lingkungan (C)
        self.mq = 120.0
        self.temp = 29.0
        self.water_pct = 100.0       # level tandon (%)
        self.flame_force = False     # sinar matahari mengenai flame sensor

        # Mode operasi: 'auto' atau 'manual'
        self.control_mode = 'auto'
        self.manual_pump = False

        # Aktuator & indikator
        self.alarm = False
        self.pump = False
        self.lamp_green = True
        self.lamp_red = False
        self.buzzer_active = False
        self.ind_flame = self.ind_smoke = self.ind_temp = False
        self.votes = 0

        # Hasil ukur
        self.t_ignite = None
        self.t_alarm = None
        self.t_out = None            # waktu api padam
        self.response = None         # api menyala -> pompa hidup (detik)
        self.ever_alarm = False
        self.lockout = False         # True selama penutupan setelah api padam (belum siaga lagi)
        self.quiet = 0.0             # detik semua sensor normal saat alarm masih aktif
        self.low_water_alerted = False

        # Status sistem & log
        self.system_state = 'SIAGA'  # SIAGA, SENSOR_PARSIAL, ALARM_POMPA, AIR_HABIS, API_PADAM, MANUAL
        self.last_telemetry_time = -10.0
        self.telemetry_log = []
        self.event_feed = []
        self.last_alert_state = None
        self.scenario_id = None

    def ignite(self):
        """Nyalakan api uji (lilin)."""
        if not self.burning and not self.lockout:
            self.burning = True
            self.fire = max(self.fire, 0.02)
            self.t_ignite = self.t
            self.t_out = None        # ukuran baru untuk api baru
            self.t_alarm = None
            self.response = None

    def update_physics(self, dt):
        """Dinamika api dan respons sensor (orde pertama)."""
        self.t += dt

        # Api membesar, atau mengecil saat disiram
        if self.pump:
            self.fire = max(0.0, self.fire - dt * self.FIRE_DROP)
        elif self.burning:
            self.fire = min(1.0, self.fire + dt * self.FIRE_GROW)
        if self.burning and self.pump and self.fire <= 0:
            self.burning = False
            self.t_out = self.t
            self.lockout = True

        # Sensor MQ-2 dan DHT22 mengikuti kondisi
        mq_target = self.mq_base + self.fire * self.MQ_GAIN
        tp_target = self.t_base + self.fire * self.T_GAIN
        self.mq += (mq_target - self.mq) * dt * self.MQ_RATE
        self.temp += (tp_target - self.temp) * dt * self.T_RATE

        # Telemetri berkala setiap 1 detik (waktu simulasi)
        if self.t - self.last_telemetry_time >= 1.0:
            self._publish_telemetry()
            self.last_telemetry_time = self.t

    def update_logic(self, dt):
        """Implementasi alur logika maket: voting 2 dari 3 -> alarm + pompa."""
        self._check_low_water()
        if self.control_mode == 'manual':
            self.system_state = 'MANUAL'
            self.alarm = False
            self.pump = self.manual_pump
            self._drain_water(dt)
            self.lamp_green, self.lamp_red, self.buzzer_active = True, False, False
            return

        # LANGKAH 1: hitung tiga indikator
        self.ind_flame = self.fire > self.FLAME_ON or self.flame_force
        self.ind_smoke = self.mq > self.MQ_ON
        self.ind_temp = self.temp > self.TEMP_ON
        self.votes = int(self.ind_flame) + int(self.ind_smoke) + int(self.ind_temp)

        # LANGKAH 2: penutupan otomatis
        if self.lockout and self.t_out is not None:
            # a) setelah api padam oleh sprinkler: jadwal tetap
            if self.t - self.t_out > self.PUMP_OFF_DELAY:
                self.pump = False
            if self.t - self.t_out > self.ALARM_OFF_DELAY:
                self.alarm = False
        elif self.alarm:
            # b) alarm tanpa api padam (mis. alarm palsu): tutup setelah sensor normal
            self.quiet = self.quiet + dt if self.votes == 0 else 0.0
            if self.quiet > self.PUMP_OFF_DELAY:
                self.pump = False
            if self.quiet > self.ALARM_OFF_DELAY:
                self.alarm = False
                self.quiet = 0.0

        # LANGKAH 3: siaga lagi (re-arm) setelah penutupan selesai
        if self.lockout and not self.alarm and not self.pump and self.votes < self.VOTES_NEEDED:
            self.lockout = False

        # LANGKAH 4: voting >= 2 dari 3 -> ALARM + POMPA MENYALA
        if not self.lockout and self.votes >= self.VOTES_NEEDED:
            self.quiet = 0.0
            if not self.alarm:
                self.alarm = True
                self.ever_alarm = True
                self.t_alarm = self.t
                if self.burning and self.t_ignite is not None:
                    self.response = self.t - self.t_ignite
                resp = "-" if self.response is None else "%.1f dtk" % self.response
                self._send_alert('ALARM', "🚨 ALARM: voting %d dari 3 (api=%d asap=%d suhu=%d). "
                                 "Pompa menyala, respons %s." % (
                                     self.votes, self.ind_flame, self.ind_smoke, self.ind_temp, resp))
            if self.water_pct > self.WATER_MIN:
                self.pump = True

        # LANGKAH 5: air tandon berkurang, pompa berhenti di level minimum
        self._drain_water(dt)

        # Status sistem & indikator
        if self.lockout:
            if self.alarm or self.pump:
                self.system_state = 'API_PADAM'
                self._send_alert('API_PADAM', "✅ API PADAM: pompa dan alarm menutup otomatis.")
            else:
                self.system_state = 'SIAGA'
        elif self.pump:
            self.system_state = 'ALARM_POMPA'
        elif self.alarm:
            self.system_state = 'AIR_HABIS' if self.water_pct <= self.WATER_MIN else 'MENUTUP'
            if self.system_state == 'MENUTUP':
                self._send_alert('MENUTUP', "ℹ️ Sensor kembali normal: pompa dan alarm menutup otomatis.")
        elif self.votes >= 1:
            self.system_state = 'SENSOR_PARSIAL'
        else:
            self.system_state = 'SIAGA'
        if self.system_state == 'SIAGA' and self.last_alert_state not in (None, 'SIAGA'):
            self._send_alert('SIAGA', "ℹ️ SIAGA: sistem kembali normal.")

        self.lamp_green = not self.alarm
        self.lamp_red = self.alarm
        self.buzzer_active = self.alarm

    def _drain_water(self, dt):
        if self.pump:
            # level tidak dinaikkan paksa jika awalnya sudah di bawah batas minimum
            self.water_pct = max(min(self.water_pct, self.WATER_MIN), self.water_pct - dt * self.WATER_DRAIN)
        if self.pump and self.water_pct <= self.WATER_MIN:
            self.pump = False
            self._send_alert('AIR_HABIS', "⚠️ AIR TANDON HABIS (%.0f%%). Pompa berhenti, alarm tetap aktif."
                             % self.water_pct)

    def _check_low_water(self):
        """Peringatan dini: air tandon di bawah WATER_WARN (sebelum benar-benar habis)."""
        if self.water_pct < self.WATER_WARN and not self.low_water_alerted:
            self.low_water_alerted = True
            timestr = datetime.datetime.now().strftime("%H:%M:%S")
            self.event_feed.insert(0, "[%s] ⚠️ PERINGATAN: air tandon tinggal %.0f%%. Segera isi ulang."
                                   % (timestr, self.water_pct))
            if len(self.event_feed) > 15:
                self.event_feed.pop()
        elif self.water_pct >= self.WATER_WARN:
            self.low_water_alerted = False

    def _publish_telemetry(self):
        timestr = datetime.datetime.now().strftime("%H:%M:%S")
        payload = {
            "device": "fireguard-01",
            "state": self.system_state,
            "votes": self.votes,
            "flame": int(self.ind_flame),
            "smoke_ppm": round(self.mq),
            "temp_c": round(self.temp, 1),
            "water_pct": round(self.water_pct),
            "low_water": int(self.water_pct < self.WATER_WARN),
            "pump": int(self.pump),
            "alarm": int(self.alarm),
            "time": timestr,
        }
        self.telemetry_log.insert(0, "[%s] fireguard/fireguard-01/live -> %s" % (timestr, json.dumps(payload)))
        if len(self.telemetry_log) > 20:
            self.telemetry_log.pop()

    def _send_alert(self, state, text):
        if self.last_alert_state != state:
            self.last_alert_state = state
            timestr = datetime.datetime.now().strftime("%H:%M:%S")
            self.event_feed.insert(0, "[%s] %s" % (timestr, text))
            if len(self.event_feed) > 15:
                self.event_feed.pop()

    def apply_scenario(self, scenario_id):
        """Set skenario demonstrasi sekali klik untuk presentasi."""
        self.reset()
        self.scenario_id = scenario_id

        if scenario_id == 1:        # Api sungguhan
            self.ignite()
        elif scenario_id == 2:      # Tanpa api (kondisi normal)
            pass
        elif scenario_id == 3:      # Asap masak: asap + suhu naik sedikit, tanpa api
            self.mq_base, self.t_base = 450.0, 33.0
        elif scenario_id == 4:      # Sinar matahari mengenai flame sensor
            self.flame_force = True
        elif scenario_id == 5:      # Hari panas 44 C
            self.t_base = 44.0
        elif scenario_id == 6:      # Api, tetapi tandon hampir kosong
            self.water_pct = 8.0
            self.ignite()
        elif scenario_id == 7:      # Masak + panas: asap dan suhu tinggi, tanpa api
            self.mq_base, self.t_base = 450.0, 44.0

    def evaluate(self):
        """Hasil uji skenario -> (teks, selesai?)."""
        sid, el = self.scenario_id, self.t
        if sid is None:
            return "-", False
        if sid == 1:
            if self.t_out is not None and not self.alarm and not self.pump:
                ok = self.response is not None and self.response <= 10
                return ("LULUS (respons %.1f dtk)" % self.response if ok else "GAGAL"), True
            return "Berjalan...", False
        if sid == 6:
            if self.alarm and not self.pump and self.water_pct <= self.WATER_MIN and el > 4:
                return "LULUS (alarm tetap, pompa berhenti)", True
            return "Berjalan...", False
        if el > 15:
            return ("GAGAL (alarm palsu)" if self.ever_alarm else "LULUS (tanpa alarm palsu)"), True
        return "Berjalan...", False


# =============================================================================
# 2. ANTARMUKA DESKTOP TKINTER (SIMULASI GRAFIS & SCADA CONTROLLER)
# =============================================================================
class FireGuardSimulatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Simulasi Otomasi Sprinkler & Deteksi Kebakaran - Kelompok 06 Teknik Industri UNS")
        self.root.geometry("1180x740")
        self.root.configure(bg="#0f172a")

        self.controller = FireGuardController()
        self.last_update = time.time()
        self.drops = []      # partikel air  [x, y, vx, vy]
        self.smoke = []      # partikel asap [x, y, umur]
        self._syncing = False

        self.setup_ui()
        self.update_loop()

    def setup_ui(self):
        # 1. Header Banner
        header = tk.Frame(self.root, bg="#1e293b", padx=16, pady=10, relief="groove", bd=1)
        header.pack(fill="x", side="top")

        title_lbl = tk.Label(header, text="Sistem Sprinkler & Pendeteksi Kebakaran Otomatis (FireGuard) Berbasis ESP32 & IoT",
                             font=("Segoe UI", 14, "bold"), fg="#fb923c", bg="#1e293b")
        title_lbl.pack(anchor="w")

        sub_lbl = tk.Label(header, text="Tugas Besar Otomasi Sistem • Kelompok 06 • Dosen: Prof. Dr.Eng. Pringgo W. L. & Prof. Dr. Lobes H.",
                           font=("Segoe UI", 9), fg="#94a3b8", bg="#1e293b")
        sub_lbl.pack(anchor="w")

        btn_open_web = tk.Button(header, text="🌐 Buka Versi Maket 3D", font=("Segoe UI", 9, "bold"),
                                 bg="#0284c7", fg="white", activebackground="#0369a1", cursor="hand2",
                                 padx=10, pady=4, relief="flat", command=self.open_web_browser)
        btn_open_web.place(relx=1.0, rely=0.5, anchor="e", x=-10)

        # 2. Skenario Demo Bar (Atas)
        scen_bar = tk.Frame(self.root, bg="#162030", padx=12, pady=6)
        scen_bar.pack(fill="x", side="top", pady=4)

        tk.Label(scen_bar, text="▶ Skenario Cepat Presentasi:", font=("Segoe UI", 9, "bold"),
                 fg="#cbd5e1", bg="#162030").pack(side="left", padx=6)

        for sid, name in SCENARIO_NAMES.items():
            btn = tk.Button(scen_bar, text="%d. %s" % (sid, name), font=("Segoe UI", 8, "bold"), bg="#334155",
                            fg="white", activebackground="#fb923c", padx=8, pady=3, relief="raised", bd=1,
                            command=lambda s=sid: self.on_scenario(s))
            btn.pack(side="left", padx=4)

        # 3. Main Split Container
        main_frame = tk.Frame(self.root, bg="#0f172a", padx=12, pady=6)
        main_frame.pack(fill="both", expand=True)

        # Sisi Kiri: Visual Canvas Maket & Fisik
        left_frame = tk.Frame(main_frame, bg="#1e293b", relief="groove", bd=1)
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))

        tk.Label(left_frame, text="Visualisasi Fisik Maket Sprinkler Otomatis",
                 font=("Segoe UI", 10, "bold"), fg="#f8fafc", bg="#1e293b", pady=6).pack()

        self.canvas = tk.Canvas(left_frame, width=640, height=430, bg="#0b1322", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=4)

        # Telemetry Strip Bawah Canvas
        tele_frame = tk.Frame(left_frame, bg="#151e2e", pady=6)
        tele_frame.pack(fill="x", side="bottom")

        self.lbl_tele_level = tk.Label(tele_frame, text="Air: 100 %", font=("Segoe UI", 10, "bold"), fg="#38bdf8", bg="#151e2e")
        self.lbl_tele_level.pack(side="left", expand=True)
        self.lbl_tele_temp = tk.Label(tele_frame, text="Suhu: 29.0 °C", font=("Segoe UI", 10, "bold"), fg="#fb923c", bg="#151e2e")
        self.lbl_tele_temp.pack(side="left", expand=True)
        self.lbl_tele_smoke = tk.Label(tele_frame, text="Asap: 120 ppm", font=("Segoe UI", 10, "bold"), fg="#a8a29e", bg="#151e2e")
        self.lbl_tele_smoke.pack(side="left", expand=True)
        self.lbl_tele_votes = tk.Label(tele_frame, text="Voting: 0 / 3", font=("Segoe UI", 10, "bold"), fg="#34d399", bg="#151e2e")
        self.lbl_tele_votes.pack(side="left", expand=True)

        # Sisi Kanan: Panel Kontrol SCADA & Status
        right_frame = tk.Frame(main_frame, bg="#1e293b", width=480, relief="groove", bd=1, padx=12, pady=10)
        right_frame.pack(side="right", fill="both", padx=(6, 0))
        right_frame.pack_propagate(False)

        # Status & Timer Display
        status_box = tk.Frame(right_frame, bg="#0f172a", relief="ridge", bd=2, padx=10, pady=8)
        status_box.pack(fill="x", pady=(0, 8))

        tk.Label(status_box, text="STATUS SISTEM (ESP32):", font=("Segoe UI", 8, "bold"), fg="#94a3b8", bg="#0f172a").pack(anchor="w")
        self.lbl_system_status = tk.Label(status_box, text="SIAGA (SISTEM NORMAL)", font=("Segoe UI", 13, "bold"), fg="#22c55e", bg="#0f172a")
        self.lbl_system_status.pack(anchor="w")

        self.lbl_votes = tk.Label(status_box, text="Voting: api ✗  asap ✗  suhu ✗", font=("Segoe UI", 9), fg="#cbd5e1", bg="#0f172a")
        self.lbl_votes.pack(anchor="w")

        self.lbl_timer_status = tk.Label(status_box, text="Timer penutupan: Inaktif", font=("Segoe UI", 9), fg="#eab308", bg="#0f172a")
        self.lbl_timer_status.pack(anchor="w")

        self.lbl_verdict = tk.Label(status_box, text="Hasil uji: -", font=("Segoe UI", 9, "bold"), fg="#94a3b8", bg="#0f172a")
        self.lbl_verdict.pack(anchor="w")

        # Sliders Kontrol
        ctl_box = tk.LabelFrame(right_frame, text=" Parameter Simulasi Sensor ", font=("Segoe UI", 9, "bold"),
                                fg="#7dd3fc", bg="#1e293b", padx=8, pady=6)
        ctl_box.pack(fill="x", pady=4)

        tk.Label(ctl_box, text="Level Air Tandon (0 - 100 %):", font=("Segoe UI", 8), fg="#cbd5e1", bg="#1e293b").pack(anchor="w")
        self.scale_level = tk.Scale(ctl_box, from_=0, to=100, resolution=1, orient="horizontal",
                                    bg="#1e293b", fg="white", highlightthickness=0, troughcolor="#334155",
                                    command=self.on_scale_level)
        self.scale_level.set(100)
        self.scale_level.pack(fill="x")

        tk.Label(ctl_box, text="Suhu Lingkungan (20 - 70 °C):", font=("Segoe UI", 8), fg="#cbd5e1", bg="#1e293b").pack(anchor="w")
        self.scale_temp = tk.Scale(ctl_box, from_=20, to=70, resolution=1, orient="horizontal",
                                   bg="#1e293b", fg="white", highlightthickness=0, troughcolor="#334155",
                                   command=self.on_scale_temp)
        self.scale_temp.set(29)
        self.scale_temp.pack(fill="x")

        tk.Label(ctl_box, text="Asap Lingkungan MQ-2 (0 - 1000 ppm):", font=("Segoe UI", 8), fg="#cbd5e1", bg="#1e293b").pack(anchor="w")
        self.scale_smoke = tk.Scale(ctl_box, from_=0, to=1000, resolution=10, orient="horizontal",
                                    bg="#1e293b", fg="white", highlightthickness=0, troughcolor="#334155",
                                    command=self.on_scale_smoke)
        self.scale_smoke.set(120)
        self.scale_smoke.pack(fill="x")

        # Aksi manual: api uji & reset
        act_box = tk.LabelFrame(right_frame, text=" Aksi Manual ", font=("Segoe UI", 9, "bold"),
                                fg="#7dd3fc", bg="#1e293b", padx=8, pady=6)
        act_box.pack(fill="x", pady=4)

        act_frame = tk.Frame(act_box, bg="#1e293b")
        act_frame.pack(fill="x")
        tk.Button(act_frame, text="🔥 Nyalakan Api Uji", font=("Segoe UI", 8, "bold"), bg="#c2410c", fg="white",
                  activebackground="#ea580c", relief="flat", padx=8, pady=3,
                  command=self.controller.ignite).pack(side="left", expand=True, padx=2)
        tk.Button(act_frame, text="↺ Reset", font=("Segoe UI", 8, "bold"), bg="#334155", fg="white",
                  activebackground="#475569", relief="flat", padx=8, pady=3,
                  command=self.on_reset).pack(side="left", expand=True, padx=2)

        # Uji gangguan: sinar matahari mengenai flame sensor
        fault_box = tk.Frame(right_frame, bg="#1e293b")
        fault_box.pack(fill="x", pady=4)

        self.var_flame = tk.BooleanVar(value=False)
        chk_flame = tk.Checkbutton(fault_box, text="☀ Simulasikan Sinar Matahari pada Flame Sensor",
                                   variable=self.var_flame, bg="#1e293b", fg="#eab308",
                                   selectcolor="#0f172a", font=("Segoe UI", 8, "bold"),
                                   command=self.on_toggle_flame)
        chk_flame.pack(anchor="w")

        # Log Event / Notifikasi
        tg_box = tk.LabelFrame(right_frame, text=" Log Event & Notifikasi Petugas ", font=("Segoe UI", 8, "bold"),
                               fg="#38bdf8", bg="#1e293b", padx=6, pady=4)
        tg_box.pack(fill="both", expand=True, pady=4)

        self.txt_feed = tk.Text(tg_box, height=4, bg="#090d16", fg="#e2e8f0", font=("Consolas", 8),
                                wrap="word", relief="flat", padx=6, pady=4)
        self.txt_feed.pack(fill="both", expand=True)

    # ---------------- Callback ----------------
    def sync_widgets(self):
        self._syncing = True
        self.scale_level.set(self.controller.water_pct)
        self.scale_temp.set(self.controller.t_base)
        self.scale_smoke.set(self.controller.mq_base)
        self.var_flame.set(self.controller.flame_force)
        self._syncing = False

    def on_scenario(self, sid):
        self.controller.apply_scenario(sid)
        self.drops.clear()
        self.smoke.clear()
        self.sync_widgets()

    def on_reset(self):
        self.controller.reset()
        self.drops.clear()
        self.smoke.clear()
        self.sync_widgets()

    def on_scale_level(self, val):
        if not self._syncing:
            self.controller.water_pct = float(val)

    def on_scale_temp(self, val):
        if not self._syncing:
            self.controller.t_base = float(val)

    def on_scale_smoke(self, val):
        if not self._syncing:
            self.controller.mq_base = float(val)

    def on_toggle_flame(self):
        self.controller.flame_force = self.var_flame.get()

    def open_web_browser(self):
        """Buka maket 3D (HTML) di browser secara otomatis."""
        html_path = find_html()
        if html_path:
            webbrowser.open(pathlib.Path(html_path).as_uri())
        else:
            print("File maket 3D (.html) tidak ditemukan di folder skrip ini.")

    def update_loop(self):
        now = time.time()
        dt = min(0.1, now - self.last_update)
        self.last_update = now

        # Update controller & physics
        self.controller.update_physics(dt)
        self.controller.update_logic(dt)

        # Update visual canvas
        self.render_canvas(dt)

        # Update UI texts
        self.update_ui_text()

        self.root.after(33, self.update_loop)

    # ---------------- Gambar ----------------
    @staticmethod
    def _blend(c1, c2, k):
        k = max(0.0, min(1.0, k))
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))

    def render_canvas(self, dt):
        c = self.canvas
        ctl = self.controller
        c.delete("all")

        w = c.winfo_width() or 640
        h = c.winfo_height() or 430
        clock = time.time()

        gy = int(h * 0.80)                       # garis dasar
        c.create_rectangle(0, 0, w, h, fill="#1e293b", outline="")
        c.create_rectangle(0, gy, w, h, fill="#7a5a36", outline="#5b4226")   # papan dasar maket

        # 1. Tandon air + pompa + sensor ultrasonik
        tx0, tx1, ty0 = int(w * 0.07), int(w * 0.22), int(h * 0.40)
        c.create_rectangle(tx0, ty0, tx1, gy, fill="#0b1322", outline="#7fc0f5", width=2)
        water_y = gy - (ctl.water_pct / 100.0) * (gy - ty0)
        pts = [tx0, gy, tx0, water_y]
        for x in range(tx0, tx1 + 1, 8):
            pts.extend([x, water_y + math.sin(x * 0.12 + clock * 3) * 1.5])
        pts.extend([tx1, gy])
        c.create_polygon(pts, fill="#2e86de", outline="")
        c.create_text((tx0 + tx1) // 2, ty0 + 14, text="TANDON %d%%" % round(ctl.water_pct), fill="#e2e8f0",
                      font=("Segoe UI", 8, "bold"))
        c.create_rectangle(tx0 + 14, ty0 - 24, tx1 - 14, ty0 - 8, fill="#1f6fb5", outline="#7fc0f5")
        c.create_text((tx0 + tx1) // 2, ty0 - 34, text="HC-SR04", fill="#94a3b8", font=("Segoe UI", 7))
        for m in (0, 25, 50, 75, 100):                                   # mistar level
            my = gy - (m / 100.0) * (gy - ty0)
            c.create_line(tx1 + 2, my, tx1 + 9, my, fill="#94a3b8")
            c.create_text(tx1 + 12, my, text=str(m), fill="#94a3b8", font=("Consolas", 7), anchor="w")

        px = tx0 - 22                                                    # pompa celup
        c.create_rectangle(px - 9, gy - 36, px + 9, gy, fill="#4cc38a" if ctl.pump else "#2b2f36", outline="#888f99")
        c.create_text(px, gy + 12, text="Pompa", fill="#e2e8f0", font=("Segoe UI", 7))

        # 2. Pipa dan nozzle
        pipe_y = int(h * 0.10)
        nx, ny = int(w * 0.76), int(h * 0.20)
        c.create_line(px, gy - 36, px, pipe_y, nx, pipe_y, nx, ny, fill="#3a3f48", width=8, joinstyle="round")
        if ctl.pump:
            c.create_line(px, gy - 36, px, pipe_y, nx, pipe_y, nx, ny, fill="#2e86de", width=3, joinstyle="round")
        c.create_polygon(nx - 10, ny, nx + 10, ny, nx, ny + 18, fill="#d0a23a", outline="")
        c.create_text(nx + 16, ny + 6, text="Nozzle spray", fill="#94a3b8", anchor="w", font=("Segoe UI", 7))

        # 3. Titik api uji: wadah, lilin, api
        base_y = gy - 58
        c.create_rectangle(nx - 50, gy - 8, nx + 50, gy, fill="#3a3a3a", outline="#64748b")
        c.create_rectangle(nx - 8, base_y, nx + 8, gy - 8, fill="#f1e6cf", outline="#a8a29e")
        flame_top = base_y
        if ctl.fire > 0.01:
            f = ctl.fire * (1 + 0.12 * math.sin(clock * 18) + 0.08 * math.sin(clock * 31))
            hf, wf = 14 + 80 * f, 6 + 18 * f
            flame_top = base_y - hf
            c.create_polygon(nx - wf, base_y, nx - wf * 0.6, base_y - hf * 0.5, nx, base_y - hf,
                             nx + wf * 0.6, base_y - hf * 0.5, nx + wf, base_y,
                             fill="#ff6a1a", outline="", smooth=True)
            c.create_polygon(nx - wf * 0.5, base_y, nx - wf * 0.3, base_y - hf * 0.35, nx, base_y - hf * 0.65,
                             nx + wf * 0.3, base_y - hf * 0.35, nx + wf * 0.5, base_y,
                             fill="#ffe066", outline="", smooth=True)
        c.create_text(nx, gy + 12, text="Titik api uji", fill="#e2e8f0", font=("Segoe UI", 7))

        # 4. Partikel air (semprotan) dan asap
        if ctl.pump:
            for _ in range(3):
                self.drops.append([nx, ny + 18, random.uniform(-35, 35), random.uniform(30, 80)])
        for d in self.drops:
            d[3] += 420 * dt
            d[0] += d[2] * dt
            d[1] += d[3] * dt
        self.drops = [d for d in self.drops if d[1] < base_y][-300:]
        for d in self.drops:
            c.create_oval(d[0] - 2, d[1] - 2, d[0] + 2, d[1] + 2, fill="#7fc8ff", outline="")

        smoke_level = max(0.0, min(1.0, (ctl.mq - 120.0) / 650.0))
        if random.random() < smoke_level * 0.9:
            self.smoke.append([nx + random.uniform(-10, 10), flame_top, 0.0])
        for s in self.smoke:
            s[1] -= 40 * dt
            s[0] += math.sin(s[2] * 3) * 10 * dt
            s[2] += dt
        self.smoke = [s for s in self.smoke if s[2] < 2.2][-120:]
        for s in self.smoke:
            r = 6 + s[2] * 9
            c.create_oval(s[0] - r, s[1] - r, s[0] + r, s[1] + r,
                          fill=self._blend("#9aa3ab", "#1e293b", s[2] / 2.2), outline="")

        # 5. Matahari / panas (skenario sinar matahari dan hari panas)
        if ctl.flame_force or ctl.t_base >= 40:
            sx, sy = int(w * 0.46), int(h * 0.21)
            col = "#ffd24a" if ctl.flame_force else "#ff7a30"
            for k in range(8):
                a = k * math.pi / 4 + clock
                c.create_line(sx + math.cos(a) * 26, sy + math.sin(a) * 26,
                              sx + math.cos(a) * 36, sy + math.sin(a) * 36, fill=col, width=2)
            c.create_oval(sx - 20, sy - 20, sx + 20, sy + 20, fill=col, outline="")

        # 6. Rak sensor: Flame 1, Flame 2, MQ-2, DHT22
        top = int(h * 0.50)
        modules = [
            (0.34, "Flame 1", "#7a1f1f", ctl.ind_flame, "api" if ctl.ind_flame else "aman"),
            (0.43, "Flame 2", "#7a1f1f", ctl.ind_flame, "api" if ctl.ind_flame else "aman"),
            (0.52, "MQ-2", "#1b4f8a", ctl.ind_smoke, "%d ppm" % round(ctl.mq)),
            (0.61, "DHT22", "#e8eaec", ctl.ind_temp, "%.1f °C" % ctl.temp),
        ]
        for xr, name, color, active, reading in modules:
            x = int(w * xr)
            c.create_line(x, gy, x, top + 18, fill="#666666", width=3)
            c.create_rectangle(x - 24, top - 18, x + 24, top + 18, fill=color,
                               outline="#ef4444" if active else "#334155", width=3 if active else 1)
            if name.startswith("Flame"):
                c.create_oval(x - 7, top - 7, x + 7, top + 7, fill="#ff2200" if active else "#222222", outline="#94a3b8")
            c.create_text(x, top - 28, text=name, fill="#e2e8f0", font=("Segoe UI", 8, "bold"))
            c.create_text(x, top + 30, text=reading, fill="#ef4444" if active else "#94a3b8", font=("Consolas", 8))

        # 7. Elektronik di papan dasar: ESP32, relay, buzzer, LED
        ey0, ey1 = gy + 20, h - 12
        c.create_rectangle(int(w * 0.30), ey0, int(w * 0.43), ey1, fill="#15191f", outline="#475569")
        c.create_text(int(w * 0.365), (ey0 + ey1) // 2, text="ESP32", fill="#e2e8f0", font=("Segoe UI", 8, "bold"))
        c.create_rectangle(int(w * 0.46), ey0, int(w * 0.57), ey1, fill="#1c5fc2", outline="#475569")
        c.create_text(int(w * 0.50), (ey0 + ey1) // 2 + 6, text="Relay", fill="white", font=("Segoe UI", 8))
        c.create_oval(int(w * 0.555) - 5, ey0 + 3, int(w * 0.555) + 5, ey0 + 13,
                      fill="#33ff77" if ctl.pump else "#225533", outline="")
        blink = int(clock * 7) % 2 == 0
        on = ctl.buzzer_active and blink
        c.create_oval(int(w * 0.62) - 12, ey0 + 2, int(w * 0.62) + 12, ey0 + 26, fill="#ffaa00" if on else "#111111", outline="#475569")
        c.create_text(int(w * 0.62), ey1 + 2, text="Buzzer", fill="#94a3b8", font=("Segoe UI", 7))
        c.create_oval(int(w * 0.69) - 8, ey0 + 6, int(w * 0.69) + 8, ey0 + 22, fill="#ff2020" if (ctl.lamp_red and blink) else "#401010", outline="")
        c.create_oval(int(w * 0.75) - 8, ey0 + 6, int(w * 0.75) + 8, ey0 + 22, fill="#22aa55" if ctl.lamp_green else "#104020", outline="")
        c.create_text(int(w * 0.72), ey1 + 2, text="LED alarm / siaga", fill="#94a3b8", font=("Segoe UI", 7))

    def update_ui_text(self):
        ctl = self.controller

        # Telemetry labels
        low = ctl.water_pct < ctl.WATER_WARN
        self.lbl_tele_level.config(text="Air: %d %%%s" % (round(ctl.water_pct), "  ⚠ RENDAH" if low else ""),
                                   fg="#ef4444" if low else "#38bdf8")
        self.lbl_tele_temp.config(text="Suhu: %.1f °C" % ctl.temp)
        self.lbl_tele_smoke.config(text="Asap: %d ppm" % round(ctl.mq))
        self.lbl_tele_votes.config(text="Voting: %d / 3" % ctl.votes)

        # Status sistem
        color_map = {
            'SIAGA': ('#22c55e', 'SIAGA (SISTEM NORMAL)'),
            'SENSOR_PARSIAL': ('#38bdf8', 'SENSOR PARSIAL (1 DARI 3)'),
            'ALARM_POMPA': ('#ef4444', 'ALARM AKTIF (POMPA MENYALA)'),
            'AIR_HABIS': ('#dc2626', 'ALARM AKTIF (AIR HABIS, POMPA MATI)'),
            'API_PADAM': ('#eab308', 'API PADAM (SISTEM MENUTUP)'),
            'MENUTUP': ('#eab308', 'ALARM REDA (SISTEM MENUTUP)'),
            'MANUAL': ('#a855f7', 'MANUAL OVERRIDE'),
        }
        col, txt = color_map.get(ctl.system_state, ('#ffffff', ctl.system_state))
        self.lbl_system_status.config(text=txt, fg=col)

        mark = lambda v: "✓" if v else "✗"
        self.lbl_votes.config(text="Voting: api %s  asap %s  suhu %s" % (
            mark(ctl.ind_flame), mark(ctl.ind_smoke), mark(ctl.ind_temp)))

        # Timer penutupan setelah api padam
        if ctl.lockout and (ctl.alarm or ctl.pump):
            left_p = max(0.0, ctl.PUMP_OFF_DELAY - (ctl.t - ctl.t_out))
            left_a = max(0.0, ctl.ALARM_OFF_DELAY - (ctl.t - ctl.t_out))
            self.lbl_timer_status.config(
                text="Penutupan: pompa mati %.1fs, alarm mati %.1fs" % (left_p, left_a), fg="#eab308")
        elif ctl.alarm and ctl.quiet > 0:
            left_p = max(0.0, ctl.PUMP_OFF_DELAY - ctl.quiet)
            left_a = max(0.0, ctl.ALARM_OFF_DELAY - ctl.quiet)
            self.lbl_timer_status.config(
                text="Sensor normal: pompa mati %.1fs, alarm mati %.1fs" % (left_p, left_a), fg="#eab308")
        else:
            self.lbl_timer_status.config(text="Timer penutupan: Inaktif", fg="#64748b")

        # Hasil uji skenario
        text, _ = ctl.evaluate()
        vcol = "#22c55e" if text.startswith("LULUS") else "#ef4444" if text.startswith("GAGAL") else "#94a3b8"
        self.lbl_verdict.config(text="Hasil uji: " + text, fg=vcol)

        # Log event
        current_text = self.txt_feed.get("1.0", tk.END).strip()
        new_feed_text = "\n".join(ctl.event_feed[:4])
        if current_text != new_feed_text:
            self.txt_feed.delete("1.0", tk.END)
            if new_feed_text:
                self.txt_feed.insert("1.0", new_feed_text)


# =============================================================================
# 3. MODE TEKS (--cli): UJI 6 SKENARIO TANPA JENDELA
# =============================================================================
def run_headless(sid, dt=0.05, duration=30.0, log=None):
    ctl = FireGuardController()
    ctl.apply_scenario(sid)
    next_log = 0.0
    while ctl.t < duration:
        ctl.update_physics(dt)
        ctl.update_logic(dt)
        if log is not None and ctl.t >= next_log:
            next_log += 0.5
            log.append(dict(skenario=SCENARIO_NAMES[sid], detik=round(ctl.t, 2), api=int(ctl.ind_flame),
                            mq_ppm=round(ctl.mq), suhu_c=round(ctl.temp, 1), votes=ctl.votes,
                            alarm=int(ctl.alarm), pompa=int(ctl.pump), level_air=round(ctl.water_pct)))
        _, final = ctl.evaluate()
        if final:
            break
    return ctl


def cli_main():
    fmt = lambda v: "-" if v is None else "%.1f" % v
    log = [] if "--csv" in sys.argv else None
    print("%-16s %-9s %-11s %-9s %-7s %-16s %s" % (
        "skenario", "alarm(s)", "respons(s)", "padam(s)", "air(%)", "status akhir", "hasil uji"))
    for sid, name in SCENARIO_NAMES.items():
        ctl = run_headless(sid, log=log)
        print("%-16s %-9s %-11s %-9s %-7d %-16s %s" % (
            name, fmt(ctl.t_alarm), fmt(ctl.response), fmt(ctl.t_out), round(ctl.water_pct),
            ctl.system_state, ctl.evaluate()[0]))
    if log is not None:
        i = sys.argv.index("--csv")
        path = sys.argv[i + 1] if len(sys.argv) > i + 1 else "hasil.csv"
        with open(path, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=log[0].keys())
            wr.writeheader()
            wr.writerows(log)
        print("\nCSV disimpan: %s (%d baris)" % (path, len(log)))


# =============================================================================
# 4. TITIK MASUK UTAMA (MAIN ENTRY POINT)
# =============================================================================
if __name__ == "__main__":
    # Jika dijalankan dengan argumen --web, langsung buka maket 3D di browser
    if "--web" in sys.argv:
        html_file = find_html()
        if html_file is None:
            print("File maket 3D (.html) tidak ditemukan di folder skrip ini.")
            sys.exit(1)
        print("Membuka maket 3D di browser: %s" % html_file)
        webbrowser.open(pathlib.Path(html_file).as_uri())
        sys.exit(0)

    # Mode teks tanpa jendela
    if "--cli" in sys.argv:
        cli_main()
        sys.exit(0)

    # Menjalankan Aplikasi Desktop Tkinter
    if tk is None:
        print("Tkinter tidak tersedia. Jalankan  python simulasi.py --cli  untuk mode teks.")
        sys.exit(1)
    root = tk.Tk()
    app = FireGuardSimulatorGUI(root)
    root.mainloop()
