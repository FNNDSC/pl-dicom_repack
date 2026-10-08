# pl-dicom_repack

[![Version](https://img.shields.io/docker/v/fnndsc/pl-dicom_repack?sort=semver)](https://hub.docker.com/r/fnndsc/pl-dicom_repack)
[![MIT License](https://img.shields.io/github/license/fnndsc/pl-dicom_repack)](https://github.com/FNNDSC/pl-dicom_repack/blob/main/LICENSE)
[![ci](https://github.com/FNNDSC/pl-dicom_repack/actions/workflows/ci.yml/badge.svg)](https://github.com/FNNDSC/pl-dicom_repack/actions/workflows/ci.yml)

`pl-dicom_repack` is a [_ChRIS_](https://chrisproject.org/)
_ds_ plugin which takes in a list of DICOMs as input files and
creates a single DICOM as output files.

## Abstract

This plugin takes in a list of dicom files that belong to a particular series and are
homogeneous in nature, i.e. each DICOM file must have the same shape (rows, columns,
samples per pixel, bit depth and photometric interpretation) as the rest of the files in
the series. The pixel data of every file is concatenated, in file-name order, into a
single multiframe DICOM. The header is taken from the first file in that order.

Additionally, this plugin also modifies the "NumberOfFrames" header tag of the output
DICOM with the number of frames written.

## How it works, and memory use

Memory use does not depend on the length of the series. Peak memory is roughly one
slice plus the Python/numpy/pydicom baseline (about 65 MiB measured), whether the
series has 100 or 3000 slices:

1. **Plan.** Only the *headers* are read, one file at a time. This validates that the
   slices can be merged and gives the exact size of the output.
2. **Stream.** The header is written, then each slice's pixel data is read and appended
   to the output file, and released before the next slice is read.
3. **Publish.** The file is written under a hidden `.<name>.partial` name next to its
   final location and renamed only when complete, so a failed run never leaves a
   truncated DICOM behind.

Measured on 800 slices of 512x512 16-bit (420 MB of pixel data): 863 MiB peak before,
64 MiB after, with identical pixel data.

### Compressed input

Slices stored with a compressed transfer syntax (e.g. RLE, JPEG) are decoded and the
output is written uncompressed, as *Explicit VR Little Endian*. Native (uncompressed)
slices are copied byte for byte and keep their transfer syntax. If a series mixes the two,
everything is decoded. Decoding needs the codecs listed in `requirements.txt`.
Only grayscale RLE was tested; colour images that need a colour-space conversion
(e.g. JPEG `YBR_FULL_422`) are not handled specially.

### Output location

| Input | Output |
|---|---|
| `incoming/a/b/*.dcm` | `outgoing/a/b.dcm` |
| `incoming/*.dcm` (files directly in the input directory) | `outgoing/incoming.dcm` |

### Failures

A series that cannot be merged (non-uniform slices, a truncated file, a file without pixel
data, no readable files) is reported on stderr and produces no output, while other series
are still processed. The plugin then exits with status `1`. An unreadable file inside an
otherwise good series is skipped with a message and is not counted as a frame.

### Limitations

- Slice order is by **file name**. Zero-pad numbered names (`s001.dcm`, `s010.dcm`);
  the plugin does not sort by `InstanceNumber` or slice position.
- The header comes from a single slice, so per-slice attributes such as `InstanceNumber`
  and `ImagePositionPatient` describe only the first frame. No per-frame functional groups
  are created.
- A DICOM Pixel Data element is limited to 4 GiB; larger series are rejected up front.
- Bit-packed pixel data (`BitsAllocated` = 1) is not supported.

## Installation

`pl-dicom_repack` is a _[ChRIS](https://chrisproject.org/) plugin_, meaning it can
run from either within _ChRIS_ or the command-line.

## Local Usage

To get started with local command-line usage, use [Apptainer](https://apptainer.org/)
(a.k.a. Singularity) to run `pl-dicom_repack` as a container:

```shell
apptainer exec docker://fnndsc/pl-dicom_repack dicom_repack [--args values...] input/ output/
```

To print its available options, run:

```shell
apptainer exec docker://fnndsc/pl-dicom_repack dicom_repack --help
```

## Examples

`dicom_repack` requires two positional arguments: a directory containing
input data, and a directory where to create output data.
First, create the input directory and move input data into it.

```shell
mkdir incoming/ outgoing/
mv some.dat other.dat incoming/
apptainer exec docker://fnndsc/pl-dicom_repack:latest dicom_repack [--args] incoming/ outgoing/
```

## Development

Instructions for developers.

### Building

Build a local container image:

```shell
docker build -t localhost/fnndsc/pl-dicom_repack .
```

### Running

Mount the source code `dicom_repack.py` into a container to try out changes without rebuild.

```shell
docker run --rm -it --userns=host -u $(id -u):$(id -g) \
    -v $PWD/dicom_repack.py:/usr/local/lib/python3.11/site-packages/dicom_repack.py:ro \
    -v $PWD/in:/incoming:ro -v $PWD/out:/outgoing:rw -w /outgoing \
    localhost/fnndsc/pl-dicom_repack dicom_repack /incoming /outgoing
```

### Testing

Run unit tests using `pytest`.
It's recommended to rebuild the image to ensure that sources are up-to-date.
Use the option `--build-arg extras_require=dev` to install extra dependencies for testing.

```shell
docker build -t localhost/fnndsc/pl-dicom_repack:dev --build-arg extras_require=dev .
docker run --rm -it -v "$PWD:/app:ro" -w /app localhost/fnndsc/pl-dicom_repack:dev \
    pytest -o cache_dir=/tmp/pytest
```

The mount is needed because the image does not contain `tests/`. `tests/test_repack.py`
builds small synthetic series (native, big-endian, implicit VR, RLE-compressed, odd sizes)
and checks the output frame by frame, the failure handling, and that peak memory does not
grow with the number of slices.

## Release

Steps for release can be automated by [Github Actions](.github/workflows/ci.yml).
This section is about how to do those steps manually.

### Increase Version Number

Increase the version number in `setup.py` and commit this file.

### Push Container Image

Build and push an image tagged by the version. For example, for version `1.2.3`:

```
docker build -t docker.io/fnndsc/pl-dicom_repack:1.2.3 .
docker push docker.io/fnndsc/pl-dicom_repack:1.2.3
```

### Get JSON Representation

Run [`chris_plugin_info`](https://github.com/FNNDSC/chris_plugin#usage)
to produce a JSON description of this plugin, which can be uploaded to _ChRIS_.

```shell
docker run --rm docker.io/fnndsc/pl-dicom_repack:1.2.3 chris_plugin_info -d docker.io/fnndsc/pl-dicom_repack:1.2.3 > chris_plugin_info.json
```

Intructions on how to upload the plugin to _ChRIS_ can be found here:
https://chrisproject.org/docs/tutorials/upload_plugin

