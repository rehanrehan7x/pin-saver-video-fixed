Pin Saver
=========

A small website that runs on your own computer. Paste Pinterest pin links and
download the original-size photo or the video.

Windows quick start
-------------------
1. Install Python 3.10 or newer from python.org (tick "Add python.exe to PATH").
2. Unzip this folder anywhere.
3. Double-click run.bat
   The first run installs the packages (about a minute), then your browser
   opens at http://127.0.0.1:5000

Manual start (any OS)
---------------------
    py -m venv venv                   (Mac/Linux: python3 -m venv venv)
    venv\Scripts\activate             (Mac/Linux: source venv/bin/activate)
    pip install -r requirements.txt
    python app.py

Notes
-----
- Photos and videos both work. Videos download as .mp4 directly when Pinterest
  provides one. ffmpeg is only needed for the rare video pin that has no direct
  .mp4 (install with: winget install ffmpeg).
- If a video pin shows the tag "Photo", or a video fails, look at the black
  console window: each lookup prints a line such as
  "pin 123: ... mp4=1 hls=1 ... -> VIDEO". Send that line for troubleshooting.
- If downloads start failing, update the video helper:  pip install -U yt-dlp
- Settings via environment variables: PORT (default 5000), HOST (default
  127.0.0.1, which keeps the site private to your computer), NO_BROWSER=1.
- Files:  app.py (web server) | pinterest_core.py (Pinterest logic) |
          templates/index.html (the page)
