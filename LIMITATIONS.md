# Limitations and integration requirements

A direct MP4 download was compared with its source by hash. Live platform extraction and authenticated downloads were not covered by that check.

The local requirements include the web interface and download clients. Install FFmpeg on PATH for sources that need separate audio/video streams merged.

Keep web services bound to `127.0.0.1`. Hosting this application for multiple users requires authentication and separate storage and resource limits.
