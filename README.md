# CTI Camera Follow

Bosch CCS 1000 D → Rocware RC821U (VISCA over IP) → OBS (prin go2rtc) → Virtual Camera pentru Zoom/Teams.
Aplicație Windows independentă: fereastră proprie (WebView2), iconiță lângă ceas, installer `.exe`.

## Instalare
1. Descarci `CTI-CameraFollow-Setup-x.y.z.exe` din **Releases**.
2. Îl rulezi. Installer-ul nu e semnat, deci Windows SmartScreen afișează un avertisment: alegi **More info → Run anyway**.
3. Bifezi ce vrei: scurtătură pe Desktop, pornire automată la login și componentele care lipsesc (OBS, VB-Cable, Audio Monitor).

Installer-ul face următoarele:
- copiază aplicația în `C:\Program Files\CTI Camera Follow`;
- creează scurtăturile și regulile de firewall;
- importă config-ul de la versiunea veche cu scripturi;
- dezactivează scripturile vechi (`START.bat` etc.).

Config-ul, presetările și jurnalul stau în `C:\ProgramData\CTI Camera Follow` și rămân la update sau la reinstalare.

## Utilizare
- **Închiderea ferestrei nu oprește aplicația**: rămâne lângă ceas și urmărește în continuare camerele.
- **Meniul iconiței de lângă ceas**: Deschide, mod AUTO/MANUAL, Diagnostic / Reparare, Ieșire.
- **Diagnostic / Reparare**, buton și în fereastră:
  - caută CCS-ul în rețea dacă și-a schimbat IP-ul;
  - corectează calea RTSP a camerelor;
  - repornește și reconfigurează OBS;
  - repornește preview-ul.
- **Căutare automată CCS**: dacă CCS-ul nu răspunde 30 s, aplicația îl caută singură.
- **Interfața din rețea**: `http://IP-laptop:28080`.

## Actualizări
Aplicația verifică GitHub Releases la pornire și apoi la fiecare 6 ore. Când apare o versiune nouă, afișează bara **Actualizează acum**.
- **Repo privat**: în Configurare completezi `update.token` cu un token fine-grained, doar citire pe *Contents* al acestui repo.
- **Repo public**: nu e nevoie de token.

## Publicare versiune nouă
```
git tag v1.0.1
git push origin v1.0.1
```
GitHub Actions construiește installer-ul și îl publică automat la Releases. La push pe `main` se construiește doar un artefact de test, fără release.

## Structură
- `src/app.py`: serverul și logica (CCS, VISCA, OBS, go2rtc, reparare, actualizare).
- `src/launcher.py`: fereastra, iconița de lângă ceas, instanța unică.
- `src/static/index.html`: interfața.
- `installer/setup.iss`: installer-ul Inno Setup, cu `installer/tools/*.ps1`.
- `cti.spec`: configurația PyInstaller.
- `.github/workflows/build.yml`: build-ul automat.

## Dezvoltare
```
pip install -r requirements.txt
python src/launcher.py            # cu fereastră
python src/launcher.py --nogui    # doar serverul: http://localhost:28080
```
