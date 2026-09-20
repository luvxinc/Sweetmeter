# Bundled fonts

Unmodified Liberation 2.1.5 fonts, distributed under the included
[SIL Open Font License 1.1](LICENSE):

- `LiberationSans-Regular.ttf` and `LiberationSans-Bold.ttf`: dashboard renderer.
- `LiberationMono-Regular.ttf`: input to `scripts/generate_screen_font.py`.

Source: [upstream release 2.1.5](https://github.com/liberationfonts/liberation-fonts/releases/tag/2.1.5),
archive `liberation-fonts-ttf-2.1.5.tar.gz`.
Archive SHA-256: `7191c669bf38899f73a2094ed00f7b800553364f90e2637010a69c0e268f25d0`.

| File | SHA-256 |
| --- | --- |
| LiberationSans-Regular.ttf | `76d04c18ea243f426b7de1f3ad208e927008f961dc5945e5aad352d0dfde8ee8` |
| LiberationSans-Bold.ttf | `788abee4c806d660e8aee46689dd8540cd4bb98da03dcc9d171ce3efd99a9173` |
| LiberationMono-Regular.ttf | `f2b83c763e8afd21709333370bed4774337fae82267937e2b5aea7e2fbd922c1` |

`firmware/src/screen_font.h` is a generated ASCII subset and bitmap conversion,
named **Sweetmeter UI 6x11**, also under OFL-1.1. It replaces the prototype's
unattributed bitmap font. The copyright and license must accompany firmware
downloads as well as companion packages. The Apache project license does not
replace the font license.
