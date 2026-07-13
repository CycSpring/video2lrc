# Third-party notices

Video2LRC release packages include third-party software. This notice is provided
for attribution and license discovery; the corresponding license texts copied
from the build environment are stored in the package's `licenses` directory.
The exact Python package versions are recorded in `requirements.lock.txt`.

## Bundled media tools

The Windows release includes separate `ffmpeg.exe` and `ffprobe.exe` programs
from the Gyan FFmpeg Essentials build. That build is licensed under GPL version
3 and is invoked as a child process; it is not linked into Video2LRC.

- FFmpeg: GPL-3.0-or-later, <https://ffmpeg.org/>
- Gyan Windows build and corresponding build sources:
  <https://www.gyan.dev/ffmpeg/builds/> and
  <https://github.com/GyanD/codexffmpeg>

The package includes the distributor's GPL text and build README as
`licenses/FFmpeg-GPL-3.0.txt` and `licenses/FFmpeg-build-README.txt`.

The Windows setup UI uses the `ChineseSimplified.isl` translation from the
Inno Setup 6.7.3 source tree. Its upstream attribution header is retained in
`installer/languages/ChineseSimplified.isl`; it remains subject to the Inno
Setup license, <https://jrsoftware.org/files/is/license.txt>.

## Major runtime components

- Python: Python Software Foundation License, <https://www.python.org/>
- PySide6 Essentials and Shiboken6: LGPL-3.0-only OR GPL-2.0-only OR
  GPL-3.0-only, <https://pyside.org/>
- RapidOCR: Apache-2.0, <https://github.com/RapidAI/RapidOCR>
- ONNX Runtime: MIT, <https://onnxruntime.ai/>
- OpenCV Python: Apache-2.0, <https://github.com/opencv/opencv-python>
- NumPy: BSD-3-Clause and bundled component licenses,
  <https://numpy.org/>
- Pillow: HPND, <https://python-pillow.org/>
- Shapely: BSD-3-Clause, <https://shapely.readthedocs.io/>
- RapidFuzz: MIT, <https://github.com/rapidfuzz/RapidFuzz>
- PyYAML: MIT, <https://pyyaml.org/>
- Requests: Apache-2.0, <https://requests.readthedocs.io/>
- urllib3: MIT, <https://urllib3.readthedocs.io/>
- charset-normalizer: MIT,
  <https://github.com/jawah/charset_normalizer>
- idna: BSD-3-Clause, <https://github.com/kjd/idna>
- certifi: MPL-2.0, <https://github.com/certifi/python-certifi>
- tqdm: MPL-2.0 AND MIT, <https://tqdm.github.io/>
- FlatBuffers: Apache-2.0, <https://github.com/google/flatbuffers>
- Protocol Buffers: BSD-3-Clause,
  <https://github.com/protocolbuffers/protobuf>
- OmegaConf: BSD-3-Clause, <https://github.com/omry/omegaconf>
- pyclipper: MIT, <https://github.com/fonttools/pyclipper>

Each component remains subject to its own license. If a notice here conflicts
with a bundled license text, the bundled license text controls.
