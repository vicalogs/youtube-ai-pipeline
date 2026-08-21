# whisper.cpp executable

`whisper-cli` is the local platform-specific whisper.cpp executable. Replace it
with a binary compiled for the deployment host and keep the executable bit set.
Docker builds compile their own Linux binary and do not use this file.
