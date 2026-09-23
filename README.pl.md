# home-assistant-rtsp-camera

[![Tests](https://github.com/skydiveTom/home-assistant-rtsp-camera/actions/workflows/tests.yml/badge.svg)](https://github.com/skydiveTom/home-assistant-rtsp-camera/actions/workflows/tests.yml)
[![Licencja: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Dodawaj instancje kamer RTSP do Home Assistant **korzystając tylko z adresu
strumienia**.

Nadaj kamerze nazwę, wklej adres, naciśnij **Sprawdź stream**, aby przekonać się,
czy działa, i użyj **Szybkiego podglądu**, aby zobaczyć obraz na żywo — a potem
umieść powstałą encję `camera` na dowolnym pulpicie.

[![Otwórz sklep dodatków w swojej instancji Home Assistant.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FskydiveTom%2Fhome-assistant-rtsp-camera)
[![Otwórz repozytorium w HACS w swojej instancji Home Assistant.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=skydiveTom&repository=home-assistant-rtsp-camera&category=integration)

## Gdzie dodaję kamery?

W dodatku: otwórz panel **RTSP Cameras** (pozycja w menu po lewej, albo
*Ustawienia → Dodatki → RTSP Camera Manager → Otwórz interfejs web*), naciśnij
**Dodaj kamerę**, nadaj nazwę, wklej adres RTSP i sprawdź go przyciskami
*Sprawdź stream* oraz *Szybki podgląd* przed zapisaniem.

Integracja celowo nie ma okna „dodaj kamerę”: zamienia tylko plik kamer zapisany
przez dodatek na encje `camera`, dzięki czemu kamery zarządzasz w jednym miejscu —
w panelu dodatku.

## Co jest w repozytorium

| Ścieżka | Opis |
| --- | --- |
| `rtsp_cameras/` | **Dodatek** do Home Assistant: interfejs webowy, test strumienia, podgląd, przechowywanie kamer. |
| `custom_components/rtsp_cameras/` | **Integracja**, która zamienia każdą kamerę w encję `camera` (to instaluje HACS). |
| `rtsp_cameras/custom_components/` | Kopia integracji wysyłana w obrazie dodatku (katalog dodatku jest kontekstem budowania Dockera). |
| `scripts/sync_integration.ps1` | Utrzymuje identyczność obu kopii integracji (pilnuje tego zestaw testów). |
| `rtsp_cameras/tests/` | Testy API, magazynu danych, warstwy ffmpeg, tłumaczeń i struktury repozytorium. |

## Funkcje

- **Tylko adres URL** – bez YAML, bez skanowania ONVIF, bez aplikacji producenta.
  Obsługiwane są `rtsp://`, `rtsps://`, `rtmp://` oraz strumienie MJPEG po `http(s)://`.
- **Test streamu** – `ffprobe` pokazuje kodek, profil, rozdzielczość, liczbę klatek,
  przepływność i dokładny komunikat błędu; także dla adresów jeszcze niezapisanych.
- **Szybki podgląd** – obraz na żywo wewnątrz dodatku jako MJPEG (działa w każdej
  przeglądarce) lub HLS (kopiowanie strumienia H.264, więc Raspberry Pi pozostaje
  bez obciążenia).
- **Prawdziwe encje kamer** – `camera.<nazwa>` ze źródłem strumienia ustawionym na
  adres RTSP, więc obrazem na żywo zajmuje się natywny komponent `stream`
  (HLS/WebRTC), a miniatury generuje sam Home Assistant.
- **Działa od razu** – dodanie, zmiana nazwy, wyłączenie lub usunięcie kamery jest
  widoczne w Home Assistant w kilka sekund, bez restartu i bez YAML.
- **Cztery języki** – angielski (domyślny), niemiecki, hiszpański i polski.
  Interfejs podąża za językiem ustawionym w Home Assistant.
- **Bezpieczeństwo** – dodatek dostępny wyłącznie przez uwierzytelniony ingress
  Home Assistant, a dane logowania w adresach strumieni są maskowane w interfejsie
  i w logach.
- **Gotowy obraz** – publikowany jako obraz wieloarchitekturowy
  (`ghcr.io/skydiveTom/rtsp-cameras`) przez GitHub Actions, więc instalacja
  i aktualizacja pobierają obraz zamiast budować go na Twoim Home Assistant.

## Instalacja

### 1. Dodatek (zalecane — robi wszystko)

1. Kliknij przycisk *Dodaj repozytorium* powyżej albo dodaj

   ```
   https://github.com/skydiveTom/home-assistant-rtsp-camera
   ```

   w Ustawienia → Dodatki → Sklep z dodatkami → ⋮ → *Repozytoria*.

2. Zainstaluj **RTSP Camera Manager**, uruchom go i otwórz **RTSP Cameras**.
3. Dodaj pierwszą kamerę: nazwa, adres RTSP, **Sprawdź stream**, zapisz.
4. Zrestartuj Home Assistant, gdy pojawi się odpowiedni baner — dodatek instaluje
   integrację w `/config/custom_components/rtsp_cameras`, a encje kamer pojawiają
   się po restarcie.

### 2. Sama integracja (HACS lub ręcznie)

1. Użyj przycisku HACS powyżej, dodaj repozytorium jako *Integrację*, albo skopiuj
   `custom_components/rtsp_cameras` do `/config/custom_components/`.
2. Zrestartuj Home Assistant.
3. Ustawienia → Urządzenia i usługi → *Dodaj integrację* → **RTSP Camera Manager**
   i zaakceptuj domyślną ścieżkę pliku kamer
   (`rtsp_cameras/cameras.json` w katalogu konfiguracji).

Instalując integrację samodzielnie, ustaw w dodatku opcję `install_integration` na
`false`, aby dodatek nie nadpisywał Twojej kopii.

### 3. Testy bez kamery

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

Testy używają atrap `ffmpeg`/`ffprobe`, więc nie potrzebują prawdziwej kamery.

## Jak to działa

```
przeglądarka ──▶ interfejs dodatku (ingress)
                    │  zapis kamer
                    ▼
             /data/cameras.json ──(kopia)──▶ /config/rtsp_cameras/cameras.json
                                                     │ odczyt co 10 s
                                                     ▼
                                  custom_components/rtsp_cameras
                                                     └─▶ camera.<nazwa>
                                                         (stream_source = adres RTSP)
```

- Dodatek przechowuje listę kamer i nie pośredniczy w przekazie obrazu — strumień
  RTSP otwiera sam Home Assistant.
- `ffmpeg` w dodatku uruchamia się tylko na czas testu, miniatury i podglądu,
  a proces podglądu jest kończony wraz z zamknięciem okna.

## Dokumentacja

- Dokumentacja dodatku: [`rtsp_cameras/DOCS.md`](rtsp_cameras/DOCS.md)
- Historia zmian: [`rtsp_cameras/CHANGELOG.md`](rtsp_cameras/CHANGELOG.md)
- Wersja angielska: [`README.md`](README.md)

## Wsparcie

Błędy i propozycje funkcji:
<https://github.com/skydiveTom/home-assistant-rtsp-camera/issues>

## Licencja

GNU General Public License v3.0 — patrz [LICENSE](LICENSE).

Dodatek zawiera bibliotekę [hls.js](https://github.com/video-dev/hls.js) 1.5.20
(Apache-2.0) dla podglądu HLS w przeglądarkach bez natywnego wsparcia oraz
wykorzystuje [ffmpeg](https://ffmpeg.org/) w swoim kontenerze.
