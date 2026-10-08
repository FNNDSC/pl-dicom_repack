#!/usr/bin/env python

import os
import struct
import sys
from pathlib import Path
from argparse import ArgumentParser, Namespace, ArgumentDefaultsHelpFormatter
from dataclasses import dataclass, field

import numpy as np
from chris_plugin import chris_plugin, PathMapper
import pydicom as dicom
from pydicom.uid import ExplicitVRLittleEndian

__version__ = '1.3.0'

DISPLAY_TITLE = r"""
       _           _ _                                                 _    
      | |         | (_)                                               | |   
 _ __ | |______ __| |_  ___ ___  _ __ ___    _ __ ___ _ __   __ _  ___| | __
| '_ \| |______/ _` | |/ __/ _ \| '_ ` _ \  | '__/ _ \ '_ \ / _` |/ __| |/ /
| |_) | |     | (_| | | (_| (_) | | | | | | | | |  __/ |_) | (_| | (__|   < 
| .__/|_|      \__,_|_|\___\___/|_| |_| |_| |_|  \___| .__/ \__,_|\___|_|\_\
| |                                     ______       | |                    
|_|                                    |______|      |_|                    

"""  + "\t\t -- version " + __version__ + " --\n\n"


parser = ArgumentParser(description='A ChRIS plugin to repack slices of a multiframe dicom',
                        formatter_class=ArgumentDefaultsHelpFormatter)
parser.add_argument('-f', '--fileFilter', default='dcm', type=str,
                    help='input file filter glob')
parser.add_argument('-t', '--outputType', default='dcm', type=str,
                    help='input file filter glob')
parser.add_argument('-V', '--version', action='version',
                    version=f'%(prog)s {__version__}')


# Largest value that fits in a DICOM 32-bit element length (0xFFFFFFFF means "undefined").
MAX_ELEMENT_LENGTH = 0xFFFFFFFE


@dataclass
class SliceInfo:
    """What we need to know about one input file, learned without reading its pixels."""
    name: str
    path: Path
    transfer_syntax: object
    frames: int
    geometry: tuple


@dataclass
class SeriesPlan:
    """Everything needed to write the output, computed before any pixel is read."""
    header: dicom.Dataset                      # first slice, without PixelData
    slices: list = field(default_factory=list)
    frame_bytes: int = 0                       # bytes of ONE decoded frame
    passthrough: bool = True                   # True: copy native PixelData bytes verbatim

    @property
    def total_frames(self) -> int:
        return sum(s.frames for s in self.slices)

    @property
    def pixel_bytes(self) -> int:
        return self.total_frames * self.frame_bytes


def read_header(dicom_path):
    """Read a file's header only (no pixel data). Returns ``None`` if unreadable."""
    try:
        return dicom.dcmread(str(dicom_path), stop_before_pixels=True)
    except Exception as ex:
        print(f"Skipping unreadable file {dicom_path}: {ex}")
        return None


def is_native(ds) -> bool:
    """True if PixelData is stored as plain (unencapsulated) pixel bytes."""
    ts = getattr(getattr(ds, 'file_meta', None), 'TransferSyntaxUID', None)
    return ts is None or not ts.is_compressed


def plan_series(dir_name, dicom_list) -> SeriesPlan:
    """Validate a series from its headers alone and work out the output layout.

    Raises ``ValueError`` if the slices cannot be merged without corrupting the
    result. Memory use here is one header at a time.
    """
    plan = None
    first_geometry = None
    transfer_syntaxes = set()
    all_native = True

    for name in sorted(dicom_list):
        path = Path(dir_name) / name
        ds = read_header(path)
        if ds is None:
            continue
        for attr in ('Rows', 'Columns', 'BitsAllocated'):
            if attr not in ds:
                raise ValueError(f"{name}: missing {attr}; not an image slice")
        samples = int(ds.get('SamplesPerPixel', 1))
        bits = int(ds.BitsAllocated)
        if bits % 8:
            raise ValueError(f"{name}: BitsAllocated={bits} (bit-packed pixels) is not supported")
        native = is_native(ds)
        geometry = (int(ds.Rows), int(ds.Columns), samples, bits,
                    int(ds.get('PixelRepresentation', 0)),
                    str(ds.get('PhotometricInterpretation', '')))
        if native and samples > 1:
            geometry += (int(ds.get('PlanarConfiguration', 0)),)
        ts = getattr(getattr(ds, 'file_meta', None), 'TransferSyntaxUID', None)
        frames = int(ds.get('NumberOfFrames', 1) or 1)
        info = SliceInfo(name, path, ts, frames, geometry)

        if plan is None:
            plan = SeriesPlan(header=ds)
            plan.frame_bytes = geometry[0] * geometry[1] * geometry[2] * (bits // 8)
            first_geometry = geometry
        elif geometry[:6] != first_geometry[:6]:
            raise ValueError(
                f"Slice {name} has geometry (rows, cols, samples, bits, signed, photometric) "
                f"{geometry[:6]}, expected {first_geometry[:6]} "
                f"- slices are not uniform, aborting merge to avoid corrupt output")
        plan.slices.append(info)
        transfer_syntaxes.add(str(ts))
        all_native = all_native and native

    if plan is None:
        raise ValueError(f"no readable DICOM slices found in {dir_name}")

    # Verbatim byte copying is only valid if every slice is stored the same, native way.
    # Otherwise every slice is decoded and the output is written as Explicit VR Little Endian.
    plan.passthrough = all_native and len(transfer_syntaxes) == 1
    if plan.pixel_bytes + (plan.pixel_bytes % 2) > MAX_ELEMENT_LENGTH:
        raise ValueError(
            f"{plan.total_frames} frames need {plan.pixel_bytes} bytes of pixel data; "
            f"a native DICOM Pixel Data element cannot exceed {MAX_ELEMENT_LENGTH} bytes (4 GiB)")
    return plan


def slice_pixels(info: SliceInfo, plan: SeriesPlan):
    """Return this slice's pixel bytes (a buffer), holding at most one slice in memory.

    Native slices are returned verbatim from the file. Compressed slices are
    decoded, so the output never contains encapsulated fragments.
    """
    expected = info.frames * plan.frame_bytes
    ds = dicom.dcmread(str(info.path))
    if 'PixelData' not in ds:
        raise ValueError(f"{info.name} has no Pixel Data")
    if plan.passthrough:
        raw = ds.PixelData
        # a trailing pad byte is allowed when the length is odd
        if not (expected <= len(raw) <= expected + 1):
            raise ValueError(
                f"Slice {info.name} has {len(raw)} pixel bytes, expected {expected} "
                f"- slices are not uniform, aborting merge to avoid corrupt output")
        return memoryview(raw)[:expected]
    arr = np.ascontiguousarray(ds.pixel_array)
    arr = arr.astype(arr.dtype.newbyteorder('<'), copy=False)   # output is little endian
    buf = memoryview(arr).cast('B')
    if len(buf) != expected:
        raise ValueError(
            f"Slice {info.name} decoded to {len(buf)} bytes, expected {expected}")
    return buf


def pixel_data_element_header(ds, length: int) -> bytes:
    """Encode the (7FE0,0010) tag, VR and length for ``ds``'s encoding."""
    endian = '<' if ds.is_little_endian else '>'
    tag = struct.pack(endian + 'HH', 0x7FE0, 0x0010)
    if ds.is_implicit_VR:
        return tag + struct.pack(endian + 'L', length)
    vr = b'OW' if int(ds.BitsAllocated) > 8 else b'OB'
    return tag + vr + b'\x00\x00' + struct.pack(endian + 'L', length)


def write_series(plan: SeriesPlan, dest: Path) -> None:
    """Write the merged multiframe file to ``dest``, streaming slice by slice.

    The file is built under a hidden ``.partial`` name next to ``dest`` and
    renamed only once complete, so a failure never leaves a truncated DICOM.
    Peak memory is one slice, however long the series is.
    """
    header = plan.header
    header.NumberOfFrames = plan.total_frames
    if not plan.passthrough:
        if hasattr(header, 'file_meta'):
            header.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        header.is_little_endian, header.is_implicit_VR = True, False
        if int(header.get('SamplesPerPixel', 1)) > 1:
            header.PlanarConfiguration = 0      # decoded colour is interleaved

    length = plan.pixel_bytes
    pad = length % 2

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f".{dest.name}.partial")
    try:
        with open(partial, 'wb') as fp:
            header.save_as(fp)
            fp.write(pixel_data_element_header(header, length + pad))
            for info in plan.slices:
                print(f"Reading dicom file: --->{info.name}<---")
                fp.write(slice_pixels(info, plan))
            if pad:
                fp.write(b'\x00')
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def output_path(inputdir: Path, outputdir: Path, series_dir: Path) -> Path:
    """Where the merged file for ``series_dir`` goes.

    ``in/a/b/*.dcm`` -> ``out/a/b.dcm``. Files directly in ``in/`` go to
    ``out/<name of in/>.dcm``; the output always stays inside ``outputdir``.
    """
    rel = series_dir.relative_to(inputdir)
    if rel == Path('.'):
        return outputdir / f"{inputdir.name or 'output'}.dcm"
    return outputdir / rel.parent / f"{rel.name}.dcm"


def merge_dicom_multiframe(dir_name, dicom_list, dest: Path) -> int:
    """Merge the single-frame files ``dicom_list`` in ``dir_name`` into ``dest``.

    Returns the number of frames written.
    """
    print(f"Incoming directory location: --->{dir_name}<---")
    plan = plan_series(dir_name, dicom_list)
    write_series(plan, dest)
    return plan.total_frames


# The main function of this *ChRIS* plugin is denoted by this ``@chris_plugin`` "decorator."
# Some metadata about the plugin is specified here. There is more metadata specified in setup.py.
#
# documentation: https://fnndsc.github.io/chris_plugin/chris_plugin.html#chris_plugin
@chris_plugin(
    parser=parser,
    title='A DICOM repack plugin',
    category='',                 # ref. https://chrisstore.co/plugins
    min_memory_limit='2Gi',      # supported units: Mi, Gi
    min_cpu_limit='2000m',       # millicores, e.g. "1000m" = 1 CPU core
    min_gpu_limit=0              # set min_gpu_limit=1 to enable GPU
)
def main(options: Namespace, inputdir: Path, outputdir: Path):
    """
    *ChRIS* plugins usually have two positional arguments: an **input directory** containing
    input files and an **output directory** where to write output files. Command-line arguments
    are passed to this main method implicitly when ``main()`` is called below without parameters.

    :param options: non-positional arguments parsed by the parser given to @chris_plugin
    :param inputdir: directory containing (read-only) input files
    :param outputdir: directory where to write output files
    """

    print(DISPLAY_TITLE)

    mapper = PathMapper.file_mapper(inputdir, outputdir, glob=f"**/*.{options.fileFilter}", fail_if_empty=False)
    file_sets = {}  # input directory -> names of the files in it
    for input_file, _ in mapper:
        file_sets.setdefault(input_file.parent, []).append(input_file.name)

    failed = 0
    for series_dir, names in file_sets.items():
        dest = output_path(inputdir, outputdir, series_dir)
        try:
            frames = merge_dicom_multiframe(series_dir, names, dest)
            print(f"Saving output file: ---->{dest.name}<---- ({frames} frames)")
        except Exception as ex:  # one bad series must not stop the others
            failed += 1
            print(f"ERROR: could not repack {series_dir}: {ex}", file=sys.stderr)
    if failed:
        print(f"{failed} of {len(file_sets)} series failed; no output was written for them.",
              file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
