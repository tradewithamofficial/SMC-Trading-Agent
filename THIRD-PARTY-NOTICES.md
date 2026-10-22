# Third-party notices and artwork status

The project's [MIT license](LICENSE) covers its own code. It does not replace
third-party licenses, Microsoft runtime terms, service terms or artwork rights.
Keep this file and `third_party_licenses/` with a binary distribution.

## Installed components inspected locally

Versions below describe this build environment, not an assertion about every
available release. Original license files were copied from the installed
distributions/Python runtime without editing their text.

| Component | Local version / scope | License evidence |
| --- | --- | --- |
| CustomTkinter | 6.0.0; UI, themes, shapes font | [Included MIT text](third_party_licenses/CustomTkinter-LICENSE.txt); copyright 2023 Tom Schimansky |
| Pillow / PIL | 13.0.0, bundled image extensions | [MIT-CMU and bundled codec notices](third_party_licenses/Pillow-LICENSE.txt) |
| darkdetect | 0.8.0 | [BSD-3-Clause](third_party_licenses/darkdetect-LICENSE.txt) |
| packaging | 26.3 | [License choice](third_party_licenses/packaging-LICENSE.txt), [Apache-2.0](third_party_licenses/packaging-LICENSE.APACHE.txt) or [BSD-2-Clause](third_party_licenses/packaging-LICENSE.BSD.txt) |
| Python runtime | 3.14.7 | [Python and included component terms](third_party_licenses/Python-LICENSE.txt), including libffi, bzip2, Zstandard and Microsoft Distributable Code terms |
| Tcl / Tk | 9.0.4 | [Tcl](third_party_licenses/tcl-license.terms.txt), [Tk](third_party_licenses/tk-license.terms.txt), extracted from the installed runtime archives |
| Roboto Regular / Medium fonts | Bundled unchanged in CustomTkinter | Embedded font notices: Copyright 2011 Google Inc. All Rights Reserved; Apache-2.0, [full terms](third_party_licenses/packaging-LICENSE.APACHE.txt) |
| OpenSSL | Runtime reports 3.5.7 | [Apache-2.0 terms](third_party_licenses/packaging-LICENSE.APACHE.txt); [upstream version license](https://github.com/openssl/openssl/blob/openssl-3.5.7/LICENSE.txt) |
| zlib-ng compatibility runtime | Reports `1.3.1.zlib-ng`; Python 3.14.7's versioned build manifest identifies zlib-ng 2.2.4 | [zlib notice](third_party_licenses/zlib-ng-LICENSE.txt), [versioned Python dependency source](https://github.com/python/cpython-source-deps/blob/zlib-ng-2.2.4/LICENSE.md) |
| zlib DLL | `zlib1.dll` file version 1.3.2 | [Version-matched zlib notice](third_party_licenses/zlib-LICENSE.txt), [upstream](https://github.com/madler/zlib/blob/v1.3.2/LICENSE); distinct from Python's zlib-ng runtime |
| LibTomMath | Tcl dependency; bundled DLL is byte-identical to the installed Python copy | [Public-domain dedication/license](third_party_licenses/LibTomMath-LICENSE.txt) from [Python's Tcl 9.0.4 source](https://github.com/python/cpython-source-deps/blob/tcl-9.0.4.0/libtommath/LICENSE); standalone DLL version is not advertised |
| LZMA / xz runtime | Python 3.14.7's manifest identifies xz 5.2.5; `_lzma.pyd` matches installed Python | [Versioned COPYING](third_party_licenses/XZ-5.2.5-COPYING.txt): liblzma public domain; command-line tools/scripts are not bundled |
| mpdecimal | `_decimal` reports libmpdec 4.0.0; binary matches installed Python | [Required BSD-2-Clause notice](third_party_licenses/mpdecimal-LICENSE.txt), copied from the [versioned source](https://github.com/python/cpython-source-deps/blob/mpdecimal-4.0.0/COPYRIGHT.txt) |
| PyInstaller | 6.22.3, build tool/bootloader | [COPYING](third_party_licenses/PyInstaller-COPYING.txt.txt), GPL with bootloader exception and certain Apache files |
| PyInstaller community hooks | 2026.8, build tooling | [Original license](third_party_licenses/PyInstaller-hooks-LICENSE.txt); runtime hooks have separate Apache terms |

OpenSSL attribution: Copyright (c) 1998–2026 The OpenSSL Project Authors;
Copyright (c) 1995–1998 Eric A. Young, Tim J. Hudson. All rights reserved.
See the [version's upstream README](https://github.com/openssl/openssl/blob/openssl-3.5.7/README.md).
Pillow's full license bundle includes its codec/transitive notices. This software
is based in part on the work of the Independent JPEG Group. For any included
FreeType portions of Pillow, this software is based in part on the work of the
FreeType Team; its complete license is retained in Pillow's notice bundle.
Roboto license wording was read from both actual TTF name tables; the upstream
[Roboto Apache license](https://github.com/googlefonts/roboto-2/blob/main/LICENSE)
supports that attribution. The custom shapes font is supplied by CustomTkinter.

CustomTkinter's installed metadata labels its license as CC0, but the actual
installed `LICENSE` says MIT, matching the [upstream license](https://github.com/TomSchimansky/CustomTkinter/blob/master/LICENSE).
The MIT copyright/permission text is retained; CC0 metadata is not treated as
permission to discard it. Resolve provenance if replacing this dependency.

[PyInstaller's exception](https://pyinstaller.org/en/stable/license.html) permits
application distribution under a chosen license while dependency terms still
apply. Build tools such as altgraph, pefile, pywin32-ctypes and setuptools are
installed as build dependencies; their source/tools are not shipped with this app.
No Codex CLI, TradingView Desktop, MCP server or external account/session data is
redistributed. Those products keep their own licenses/terms.

## Required notices versus optional acknowledgements

Keep the project MIT notice, CustomTkinter MIT, Pillow MIT-CMU/codec notices,
darkdetect BSD, the selected packaging license terms, mpdecimal BSD, Python and
Tcl/Tk terms, and Apache-2.0 terms/attributions for Roboto/OpenSSL. Complete texts
are provided in `third_party_licenses/`; this file supplies the named attributions.
Pillow's codec notices and any applicable FreeType binary acknowledgement must
not be replaced by a generic project MIT notice.

PyInstaller acknowledgement/license inclusion is **optional for its generated
application bundle under its bootloader exception**; its COPYING and the hooks
license are nevertheless retained for transparency. The LibTomMath and xz 5.2.5
liblzma public-domain texts are retained voluntarily; their acknowledgements are
not mandatory. zlib/zlib-ng binary acknowledgement is optional; preserve their
notices if redistributing their source. Roboto/OpenSSL and the BSD/MIT notices
above are not optional merely because the project itself is MIT.

The Python license's **Additional Conditions for this Windows binary build**
retain Microsoft's Distributable Code restrictions. Windows runtime files are
unmodified, are distributed for Windows, and are not relicensed as project MIT
code. Preserve those conditions and do not strip/alter Microsoft notices or imply
Microsoft endorsement. These conditions apply to the Microsoft portions, not to
the project's own source.

Step 544 verified native files against the installed Python copies and used the
[Python 3.14.7 dependency manifest](https://github.com/python/cpython/blob/v3.14.7/PCbuild/get_externals.bat)
to resolve versions, rather than treating a zlib compatibility string as its
package version. This is versioned dependency/notice evidence, not independent
cryptographic verification of the Python supplier or a vulnerability audit.

## Project artwork

The operator supplied the current rose robot PNG and explicitly approved its
conversion to `assets/TradeWithAM_Robot.ico` for this release, without redesign.
The operator also approved retaining the current transparent chart illustration
at `assets/Futuristic AI Robot with Breakout Chart.png`. These assets are packaged
at the operator's direction; no new artwork was generated during release preparation.
The PNG is included as the editable icon source; the older local ICO is excluded.
Project licensing does not replace third-party rights or service terms.
