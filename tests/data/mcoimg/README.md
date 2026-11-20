# MCOimg test vectors

These two files are the test vectors of version 3 of the MCOimg pixel image format. They
come without change from the folder `docs/mcoimg-js/tests/` of the meshcore-open repository
([HDDen/meshcore-open](https://github.com/HDDen/meshcore-open), a fork of
[zjs81/meshcore-open](https://github.com/zjs81/meshcore-open)):

- `v3-js-decoder-fixtures.json`: 39 images, one or more for each container, block
  algorithm, scan order, and local palette form.
- `v3-js-encoder-fixtures.json`: 6 images that the JavaScript encoder of the format made.

Each vector has the `im3:` text of an image, its size, its palette profile, and the palette
reference of each pixel. `tests/test_mcoimg.py` decodes each text and compares the result.
MeshTerm does not include these files in its package.

The files are under the MIT licence of the meshcore-open repository:

<!-- ste: off -->

MIT License

Copyright (c) 2025 zjs81

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

<!-- ste: on -->
