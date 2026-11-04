# Data-Otomatisasi
FireGuard adalah prototipe sistem deteksi dini kebakaran dengan sprinkler otomatis skala maket. Sistem membaca tiga sensor (api, asap, suhu), mengambil keputusan dengan logika voting 2 dari 3, lalu menyalakan alarm dan pompa air. Repositori ini berisi simulasi Python, maket 3D interaktif, dan rancangan rangkaian ESP32.

Catatan penting: hasil di repositori ini berasal dari simulasi, bukan pengukuran prototipe fisik. Angka waktu respons dan pembacaan sensor adalah keluaran model, dan belum menggantikan pengujian alat asli.

Latar belakang

Kebakaran berkembang cepat, dan api kecil yang terlambat disadari bisa menjadi kerugian besar. Sistem proteksi bersertifikat relatif mahal, sehingga banyak ruang kecil belum terlindungi. Proyek ini menguji apakah sistem berbiaya rendah (sekitar Rp350.000 sampai Rp430.000) dapat mendeteksi api dan memadamkannya secara otomatis, serta apa saja keterbatasannya.

Cara kerja
ESP32 membaca flame sensor IR, sensor asap/gas MQ-2, dan sensor suhu DHT22.
Setiap sensor dibandingkan dengan ambang batas:
api terdeteksi bila intensitas api di atas 0,35 (sensor IR aktif),
asap bila MQ-2 di atas 300 ppm,
suhu bila DHT22 di atas 42 °C.
Bila minimal 2 dari 3 sensor mendukung, alarm (buzzer dan LED merah) aktif dan relay menyalakan pompa.
Sensor ultrasonik HC-SR04 memantau level air tandon. Pompa dilarang menyala bila air tinggal 5% atau kurang, dan peringatan muncul saat air di bawah 20%.
Setelah api padam atau semua sensor kembali normal, pompa dan alarm menutup otomatis dan sistem siaga lagi.
Data dikirim ke dashboard (telemetri bergaya MQTT pada simulasi).


Keterbatasan
Hasil berasal dari simulasi dan belum divalidasi pada prototipe fisik.
Voting 2 dari 3 masih dapat memicu alarm palsu (lihat skenario 7).
Komponen hobi tidak bersertifikat dan tidak dirancang untuk keselamatan jiwa. Sistem ini hanya prototipe pembelajaran, bukan pengganti proteksi kebakaran standar.
Isi ulang tandon air masih manual.
Rencana pengembangan
Pengujian dan pengambilan data pada maket fisik.
Model klasifikasi sederhana (decision tree) dari data sensor nyata untuk mengurangi alarm palsu.
Isi ulang air otomatis dengan pengaman float switch.
Dashboard IoT real-time (Blynk atau ThingsBoard).
