# Video Downloader

Video Downloader collects videos from direct media links and supported platforms through a local web interface or a desktop window. Queue multiple URLs, follow each download, and save the results to a folder of your choice.

## Features

- Download direct video files and supported platform URLs.
- Process multiple links through a simple local interface.
- Track progress and save results to a folder chosen by the user.
- Offer both a Flask web interface and a Tkinter desktop application.

## How it works

The Flask application handles the web workflow; downloader.py provides an alternative desktop interface. Download extraction and transfer routines run in background tasks.

**Stack:** Python · Flask · Tkinter · yt-dlp

## Getting started

Use Python 3.12 and a separate virtual environment. Run the following commands from this repository's root in Windows PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-local.txt
```

The local requirements include the web interface and download clients. Install FFmpeg on PATH for sources that need separate audio/video streams merged.

### Start the application

The terminal prints the local URL and opens the browser. For the desktop interface, run `python downloader.py`. Some sources require FFmpeg or yt-dlp.

```powershell
python app.py
```

## Example workflow

Add a permitted direct MP4 link, choose an empty output folder, and download the file.

## Testing and limitations

A direct MP4 download was compared with its source by hash. Live platform extraction and authenticated downloads were not covered by that check.

See [Verification](VERIFICATION.md) for the recorded checks and [Limitations](LIMITATIONS.md) for integration requirements.

## Configuration and security

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits. Configure your own provider credentials when a feature requires them; credentials and personal data are not included. See [Security](SECURITY.md) for local configuration and reporting guidance.
