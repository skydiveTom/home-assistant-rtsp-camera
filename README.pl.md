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
| `scripts/setup_ha_venv.ps1` | Buduje jednorazowe `.venv-ha` do testów `ha_tests` (na Windows dopisuje do niego stand-iny `fcntl`/`resource`). |
| `rtsp_cameras/tests/` | Testy API, magazynu danych, warstwy ffmpeg, tłumaczeń i struktury repozytorium. |
| `ha_tests/` | Testy integracyjne na prawdziwym Home Assistant — dowodzą, że kamery stają się encjami `camera`. |

## Funkcje

- **Tylko adres URL** – bez YAML, bez skanowania ONVIF, bez aplikacji producenta.
  Obsługiwane są `rtsp://`, `rtsps://`, `rtmp://` oraz strumienie MJPEG po `http(s)://`.
- **Test streamu** – `ffprobe` pokazuje kodek, profil, rozdzielczość, liczbę klatek,
  przepływność i dokładny komunikat błędu; także dla adresów jeszcze niezapisanych.
- **Szybki podgląd** – obraz na żywo wewnątrz dodatku. Tryb jest wykrywany
  automatycznie: najpierw MJPEG, potem HLS, a zostaje tylko ten, który działa dla
  danej kamery. Gdy transport `udp` gubi pakiety (typowe przy H.265 — test
  strumienia nadal jest zielony, bo `ffprobe` czyta tylko nagłówki), podgląd sam
  przechodzi na `tcp`.
- **Prawdziwe encje kamer** – `camera.<nazwa>` ze źródłem strumienia ustawionym na
  adres RTSP, więc obrazem na żywo zajmuje się natywny komponent `stream`
  (HLS/WebRTC), a miniatury generuje sam Home Assistant. Wybrany transport RTSP
  jest przekazywany także do Home Assistant (`stream_options`), więc karta kamery
  nie gubi pakietów UDP. Możesz też podać drugi adres strumienia (np. podstrumień
  H.264 kamery H.265), gdy karta kamery w Home Assistant pozostaje czarna.
- **Sterowanie PTZ** – kamera z obrotem i zoomem dostaje w panelu sekcję PTZ z
  gotowymi zestawami komend (Axis, Dahua/Amcrest, Xiongmai, Hikvision, Foscam,
  **ONVIF (SOAP)** albo **DVRIP na porcie 34567** dla DVR-ów bez WWW — plus własne
  komendy), padem w podglądzie i presetami. Login i hasło są **przepisywane z adresu
  RTSP**, więc nie wpisujesz ich drugi raz. **Tryb testowania PTZ** sam znajdzie
  właściwy wariant: wpisz IP kamery (albo nic), jeden przycisk odpytuje wszystkie
  warianty, a ten który odpowie staje się główną obsługą — bez ruszania kamery, bo test
  wysyła tylko komendy stop. Rodzina Xiongmai **nie przyjmuje żadnego payloadu stopu**: SDK
  producenta też nie ma komendy stop — jego flaga `stop` to ta sama komenda kierunku,
  którą ten firmware czyta jako „jedź do końca osi”. Puszczona strzałka kończy się więc
  **przechwyceniem**: pozycja z chwili puszczenia trafia do presetu dodatku, a kamera do
  niego wraca. Taka kamera dostaje **kroki na strzałkach**: pozycja z chwili zapisania staje
  się krokiem osi, krótkie naciśnięcie strzałki przechodzi o jeden krok dalej, a pole kroków
  o kilka — ruch ograniczony, bez jazdy do krańca. **Przytrzymanie** strzałki nie zaczyna już
  jazdy do krańca: panel idzie po drabince szczebel po szczeblu, dopóki klawisz jest wciśnięty,
  a puszczenie kończy spacer na tym szczeblu, na którym kamera stoi (kamera, której stop
  naprawdę zatrzymuje oś, zachowuje ruch ciągły i przechwycenie). **Encja kamery w Home
  Assistant trzyma się tej samej reguły**: ruch takiej kamery idzie po drabince przez
  `continuous_duration` sekund — szczebel po szczeblu, nigdy jazdą do krańca — a kamera,
  której stop zatrzymuje oś, zachowuje własny ruch i stop. Szczebel to **cała pozycja**, więc
  krok, który przełącza osie, wraca drugą osią na skrzyżowanie drabinek: ta rodzina nie zna
  ruchu *względnego*, więc szczebel można zapisać tylko tam, gdzie kamera już stoi, a ruch
  „w miejscu” wymagałby właśnie takiego szczebla. Krok jest mimo to wysyłany, a panel mówi,
  którą oś pociągnął i o ile — odrzucanie go zostawiało martwe obie pary strzałek, gdy tylko
  użytkownik przełączał osie. Pad trzyma **współrzędne kamery** na obu drabinkach i pokazuje
  je pod strzałkami: każdy ruch przepisuje tę parę — krok zapisuje szczebel, na który doszedł,
  *oraz* szczebel skrzyżowania, na które wraca druga oś — a encja w Home Assistant pokazuje tę
  samą parę jako `ptz_step`. Szczebel, na którym jedna drabinka stoi na drugiej, jest daną
  kamery (`step_crossings` w bloku PTZ), a nie środkiem listy: szczebel dopisany później
  przesuwa środek, więc dodatek chodził o dwa szczeble naraz na drabince, która tak urosła.
  Home Assistant dostaje usługę
  `rtsp_cameras.ptz` (te same pola co `onvif.ptz`), `rtsp_cameras.ptz_home` oraz
  przycisk dla każdego presetu. Kamerę zamontowaną do góry nogami, bokiem albo za lustrem
  **odwraca się osobno na każdej osi** — po jednym przycisku w padzie podglądu (*Odwróć
  lewo/prawo*, *Odwróć góra/dół*), zapisanym przy kamerze, więc pad, encja w Home Assistant
  i wszystkie automatyzacje idą za strzałką zgodną z obrazem.
- **Kodek strumienia bez aplikacji producenta** – kamera, której podstrumień nadaje
  **H.265**, zostaje czarna w Chrome, Edge i Firefox, bo Home Assistant kopiuje strumień
  bez transkodowania. Edytor kamery odczytuje kodery **przez ONVIF**, a gdy firmware nie ma
  usługi mediów — przez protokół producenta rodziny Xiongmai, i przełącza ten od
  podstrumienia na H.264 — konfiguracja oddana przez kamerę wraca do niej ze zmienionym
  jednym słowem, a odpowiedzią jest kodek, który kamera zgłasza po zmianie.
- **Działa od razu** – dodanie, zmiana nazwy, wyłączenie lub usunięcie kamery jest
  widoczne w Home Assistant w kilka sekund, bez restartu i bez YAML.
- **Aktualizuje się sam** – zakładka Ustawienia sprawdza na żądanie nową wersję
  dodatku (przeładowanie sklepu Supervisora) i instaluje ją jednym przyciskiem.
- **Cztery języki** – angielski (domyślny), niemiecki, hiszpański i polski.
  Interfejs podąża za językiem ustawionym w Home Assistant.
- **Bezpieczeństwo** – dodatek dostępny wyłącznie przez uwierzytelniony ingress
  Home Assistant, a dane logowania w adresach strumieni są maskowane w interfejsie
  i w logach.
- **Gotowy obraz** – publikowany jako obraz wieloarchitekturowy
  (`ghcr.io/skydivetom/rtsp-cameras`) przez GitHub Actions, więc instalacja
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

Same encje kamer pokrywa drugi zestaw testów, który uruchamia prawdziwy Home
Assistant (w wersji przypiętej w CI) i sprawdza, że kamery opublikowane przez
dodatek stają się encjami `camera`:

```bash
python -m venv .venv-ha
.venv-ha/Scripts/python -m pip install pytest-homeassistant-custom-component av
.venv-ha/Scripts/python -m pytest -c ha_tests/pytest.ini ha_tests
```

Na Windows same te trzy polecenia nie dają środowiska, którego testy użyją:
jeszcze przed pierwszym testem Home Assistant importuje moduły `fcntl` i
`resource`, które istnieją tylko na Uniksie, więc `.venv-ha` musi nieść
`sitecustomize.py`, który je zastępuje. `scripts/setup_ha_venv.ps1` tworzy
środowisko *i* zapisuje ten plik; `-ShimOnly` odtwarza sam plik, w środowisku,
które już istnieje:

```powershell
pwsh -File scripts/setup_ha_venv.ps1
```

### 4. Testy na prawdziwej kamerze (end-to-end)

Jeśli masz kamerę w sieci, ten sam zestaw przejdzie całą drogę: plik dodatku →
encja `camera.<nazwa>` → strumień HA → zdekodowana klatka → JPEG. Ustaw adres
i uruchom sam plik z testami na żywo:

```bash
export RTSP_LIVE_CAMERA_URL="rtsp://user:haslo@192.168.1.28:554/stream1"   # Windows: $env:RTSP_LIVE_CAMERA_URL = "..."
.venv-ha/Scripts/python -m pytest -c ha_tests/pytest.ini ha_tests/test_live_camera.py -q
```

Bez tej zmiennej środowiskowej testy są pomijane, więc CI i każda inna maszyna
działają nadal bez sieci.

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
