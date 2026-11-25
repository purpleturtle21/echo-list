"""The one registry of audio formats EchoList handles.

Discovery (source browser, search), rescan, adoption, copying and tagging
all read this table, so a format is either fully supported or not at all.
"""

# suffix -> how its tags are written:
#   "flac"         Vorbis comments
#   "mp3"          ID3v2
#   "mp4"          MP4 atoms
#   "aac"          MP4 atoms if the file is an MP4 container, else untaggable
#                  (raw ADTS has nowhere to store tags)
#   "wav"/"aiff"   an embedded ID3v2 chunk
#   None           copied, never tagged (OGG, WMA, DSD, ...)
TAG_KINDS = {
    ".flac": "flac",
    ".mp3": "mp3",
    ".m4a": "mp4",
    ".mp4": "mp4",
    ".aac": "aac",
    ".wav": "wav",
    ".aiff": "aiff",
    ".aif": "aiff",
    ".ogg": None,
    ".wma": None,
    ".alac": None,
    ".dsf": None,
    ".dff": None,
}

AUDIO_EXTS = frozenset(TAG_KINDS)
