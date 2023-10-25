# pl-dicom_repack

[![Version](https://img.shields.io/docker/v/fnndsc/pl-dicom_repack?sort=semver)](https://hub.docker.com/r/fnndsc/pl-dicom_repack)
[![MIT License](https://img.shields.io/github/license/fnndsc/pl-dicom_repack)](https://github.com/FNNDSC/pl-dicom_repack/blob/main/LICENSE)
[![ci](https://github.com/FNNDSC/pl-dicom_repack/actions/workflows/ci.yml/badge.svg)](https://github.com/FNNDSC/pl-dicom_repack/actions/workflows/ci.yml)

`pl-dicom_repack` is a [_ChRIS_](https://chrisproject.org/)
_ds_ plugin which takes in a list of DICOMs as input files and
creates a single DICOM as output files.

## Abstract

This plugin takes in a list of dicom files that belong to a particular series and are
homogeneous in nature, i.e. each DICOM file must have the exact shape as of the rest of the 
files in the input list. We use `pydicom`library to read the `pixel_array` contained in each 
dicom file and "merge" these into a bigger array with an additional dimension.

Additionally, this plugin also modifies the "NumberOfFrames" header tag of the output 
DICOM with the number of files present in the input directory in a particular series.

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
docker run --rm -it localhost/fnndsc/pl-dicom_repack:dev pytest
```

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

