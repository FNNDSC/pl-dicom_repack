"""Tests for the streaming repack implementation.

They build tiny synthetic series with pydicom, run the real code and read the
result back, so they exercise the actual DICOM encoding rather than mocks.
RLE fixtures use a small encoder defined here, so the tests do not depend on
the pydicom version having an RLE *encoder* (2.1.2 can only decode).
"""
import struct
import tracemalloc
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian, RLELossless, generate_uid)

import dicom_repack as app

SecondaryCaptureImageStorage = '1.2.840.10008.5.1.4.1.1.7'   # absent from pydicom.uid in 2.1.2


# --------------------------------------------------------------------------- fixtures

def rle_encode_frame(arr: np.ndarray) -> bytes:
    """Valid (literal-run only) DICOM RLE encoding of one 2-D grayscale frame."""
    width = arr.dtype.itemsize
    flat = arr.astype(arr.dtype.newbyteorder('<')).tobytes()
    segments = []
    for plane in range(width - 1, -1, -1):          # most significant byte plane first
        data = flat[plane::width]
        seg = bytearray()
        for i in range(0, len(data), 128):
            run = data[i:i + 128]
            seg.append(len(run) - 1)
            seg += run
        if len(seg) % 2:
            seg.append(0)
        segments.append(bytes(seg))
    offsets, pos = [], 64
    for seg in segments:
        offsets.append(pos)
        pos += len(seg)
    header = struct.pack('<16L', len(segments), *offsets, *([0] * (15 - len(offsets))))
    return header + b''.join(segments)


def make_slice(path: Path, array: np.ndarray, *, transfer_syntax=ExplicitVRLittleEndian,
               rle=False, frames=None, name='TEST^PATIENT', instance=1) -> np.ndarray:
    """Write one DICOM file holding ``array`` (2-D, or 3-D for several frames)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = RLELossless if rle else transfer_syntax
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, meta.MediaStorageSOPInstanceUID
    ds.PatientName, ds.PatientID, ds.Modality, ds.InstanceNumber = name, 'PID', 'OT', instance
    frame_arrays = array if array.ndim == 3 else array[None]
    ds.Rows, ds.Columns = frame_arrays.shape[1:3]
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, 'MONOCHROME2'
    bits = frame_arrays.dtype.itemsize * 8
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = bits, bits, bits - 1, 0
    if frame_arrays.shape[0] > 1 or frames:
        ds.NumberOfFrames = frame_arrays.shape[0]
    if rle:
        ds.is_little_endian, ds.is_implicit_VR = True, False
        ds.add_new(0x7FE00010, 'OB', encapsulate([rle_encode_frame(f) for f in frame_arrays]))
        ds['PixelData'].is_undefined_length = True
    else:
        big = transfer_syntax == ExplicitVRBigEndian
        ds.is_little_endian = not big
        ds.is_implicit_VR = transfer_syntax == ImplicitVRLittleEndian
        dt = frame_arrays.dtype.newbyteorder('>' if big else '<')
        ds.PixelData = frame_arrays.astype(dt).tobytes()
    ds.save_as(str(path), write_like_original=False)
    return frame_arrays


def make_series(d: Path, n=5, rows=6, cols=4, dtype=np.uint16, **kw) -> np.ndarray:
    rng = np.random.default_rng(1)
    top = np.iinfo(dtype).max
    stack = []
    for i in range(n):
        a = rng.integers(0, top, size=(rows, cols), dtype=dtype)
        a[0, :] = i                                    # make slice order checkable
        stack.append(make_slice(d / f's{i:03d}.dcm', a, instance=i + 1, **kw)[0])
    return np.stack(stack)


def repack(series_dir: Path, dest: Path) -> int:
    names = [p.name for p in series_dir.glob('*.dcm')]
    return app.merge_dicom_multiframe(series_dir, names, dest)


def read_out(path: Path):
    ds = pydicom.dcmread(str(path))
    return ds, ds.pixel_array


def tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


# ----------------------------------------------------------------------- correctness

class TestRoundTrip:

    @pytest.mark.parametrize('dtype', [np.uint8, np.uint16])
    def test_native_pixels_and_header(self, tmp_path, dtype):
        expected = make_series(tmp_path / 'in', dtype=dtype)
        out = tmp_path / 'out.dcm'
        assert repack(tmp_path / 'in', out) == 5
        ds, pixels = read_out(out)
        assert ds.NumberOfFrames == 5
        assert np.array_equal(pixels, expected)
        assert ds.PatientName == 'TEST^PATIENT'
        assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian

    def test_slice_order_is_by_file_name(self, tmp_path):
        expected = make_series(tmp_path / 'in', n=4)
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        assert [int(f[0, 0]) for f in read_out(out)[1]] == [0, 1, 2, 3]
        assert np.array_equal(read_out(out)[1], expected)

    def test_implicit_vr_native(self, tmp_path):
        expected = make_series(tmp_path / 'in', transfer_syntax=ImplicitVRLittleEndian)
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        ds, pixels = read_out(out)
        assert ds.file_meta.TransferSyntaxUID == ImplicitVRLittleEndian
        assert np.array_equal(pixels, expected)

    def test_big_endian_native(self, tmp_path):
        expected = make_series(tmp_path / 'in', transfer_syntax=ExplicitVRBigEndian)
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        ds, pixels = read_out(out)
        assert ds.file_meta.TransferSyntaxUID == ExplicitVRBigEndian
        assert np.array_equal(pixels, expected)

    @pytest.mark.parametrize('n', [1, 3])
    def test_odd_byte_count_is_padded_to_even_length(self, tmp_path, n):
        # 5x5 8-bit = 25 bytes per frame; DICOM elements must have even length
        expected = make_series(tmp_path / 'in', n=n, rows=5, cols=5, dtype=np.uint8)
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        ds, pixels = read_out(out)
        assert len(ds.PixelData) % 2 == 0
        assert np.array_equal(pixels.reshape(expected.shape), expected)

    def test_multiframe_input_files_are_summed(self, tmp_path):
        rng = np.random.default_rng(2)
        frames = [make_slice(tmp_path / 'in' / f'm{i}.dcm', rng.integers(0, 4000, (2, 6, 4), dtype=np.uint16))
                  for i in range(3)]
        out = tmp_path / 'out.dcm'
        assert repack(tmp_path / 'in', out) == 6
        ds, pixels = read_out(out)
        assert ds.NumberOfFrames == 6
        assert np.array_equal(pixels, np.concatenate(frames))


class TestCompressedInput:

    def test_fixture_encoder_round_trips(self, tmp_path):
        a = np.arange(24, dtype=np.uint16).reshape(6, 4) * 150
        make_slice(tmp_path / 'x.dcm', a, rle=True)
        assert np.array_equal(pydicom.dcmread(str(tmp_path / 'x.dcm')).pixel_array, a)

    @pytest.mark.parametrize('dtype', [np.uint8, np.uint16])
    def test_rle_input_is_decoded(self, tmp_path, dtype):
        expected = make_series(tmp_path / 'in', dtype=dtype, rle=True)
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        ds, pixels = read_out(out)
        assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        assert ds.NumberOfFrames == 5
        assert np.array_equal(pixels, expected)

    def test_equal_sized_compressed_slices_are_not_silently_corrupted(self, tmp_path):
        # Regression: the previous version joined the raw encapsulated bytes. When all
        # slices compressed to the same size it exited 0 and wrote undecodable pixels.
        a = np.full((6, 4), 1234, dtype=np.uint16)
        for i in range(4):
            make_slice(tmp_path / 'in' / f's{i}.dcm', a, rle=True)
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        assert np.array_equal(read_out(out)[1], np.stack([a] * 4))

    def test_mixed_native_and_compressed_series(self, tmp_path):
        rng = np.random.default_rng(3)
        arrays = [rng.integers(0, 4000, (6, 4), dtype=np.uint16) for _ in range(4)]
        for i, a in enumerate(arrays):
            make_slice(tmp_path / 'in' / f's{i}.dcm', a, rle=bool(i % 2))
        out = tmp_path / 'out.dcm'
        repack(tmp_path / 'in', out)
        ds, pixels = read_out(out)
        assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        assert np.array_equal(pixels, np.stack(arrays))


# ------------------------------------------------------------------- invalid series

class TestInvalidSeries:

    def test_different_geometry_is_rejected_and_nothing_is_written(self, tmp_path):
        make_series(tmp_path / 'in', n=3)
        make_slice(tmp_path / 'in' / 's999.dcm', np.zeros((8, 4), dtype=np.uint16))
        out = tmp_path / 'out' / 'o.dcm'
        with pytest.raises(ValueError, match='not uniform'):
            repack(tmp_path / 'in', out)
        assert not (tmp_path / 'out').exists() or tree(tmp_path / 'out') == []

    def test_different_bit_depth_is_rejected(self, tmp_path):
        make_series(tmp_path / 'in', n=2, dtype=np.uint16)
        make_slice(tmp_path / 'in' / 's999.dcm', np.zeros((6, 4), dtype=np.uint8))
        with pytest.raises(ValueError, match='not uniform'):
            repack(tmp_path / 'in', tmp_path / 'o.dcm')

    def test_truncated_pixel_data_is_rejected_and_leaves_nothing(self, tmp_path):
        make_series(tmp_path / 'in', n=3)
        victim = tmp_path / 'in' / 's001.dcm'
        ds = pydicom.dcmread(str(victim))
        ds.PixelData = ds.PixelData[:-8]
        ds.save_as(str(victim))
        out = tmp_path / 'out'
        with pytest.raises(ValueError, match='expected'):
            repack(tmp_path / 'in', out / 'o.dcm')
        assert tree(out) == []                      # no output and no .partial file

    def test_unreadable_file_is_skipped_and_not_counted(self, tmp_path):
        expected = make_series(tmp_path / 'in', n=3)
        (tmp_path / 'in' / 's999.dcm').write_bytes(b'this is not dicom')
        out = tmp_path / 'out.dcm'
        assert repack(tmp_path / 'in', out) == 3
        ds, pixels = read_out(out)
        assert ds.NumberOfFrames == 3 and np.array_equal(pixels, expected)

    def test_no_readable_slice_is_an_error(self, tmp_path):
        (tmp_path / 'in').mkdir()
        (tmp_path / 'in' / 'a.dcm').write_bytes(b'junk')
        with pytest.raises(ValueError, match='no readable'):
            repack(tmp_path / 'in', tmp_path / 'o.dcm')

    def test_file_without_pixel_data_is_rejected(self, tmp_path):
        make_series(tmp_path / 'in', n=2)
        victim = tmp_path / 'in' / 's001.dcm'
        ds = pydicom.dcmread(str(victim))
        del ds.PixelData
        ds.save_as(str(victim))
        with pytest.raises(ValueError, match='no Pixel Data'):
            repack(tmp_path / 'in', tmp_path / 'o.dcm')

    def test_over_4gib_is_rejected_up_front(self, tmp_path, monkeypatch):
        make_series(tmp_path / 'in', n=2)
        monkeypatch.setattr(app, 'MAX_ELEMENT_LENGTH', 50)   # 2 slices x 48 B = 96 B
        with pytest.raises(ValueError, match='4 GiB'):
            repack(tmp_path / 'in', tmp_path / 'o.dcm')


class TestAtomicOutput:

    def test_failure_midway_leaves_no_partial_file(self, tmp_path, monkeypatch):
        make_series(tmp_path / 'in', n=5)
        real = app.slice_pixels
        calls = []

        def flaky(info, plan):
            calls.append(info.name)
            if len(calls) == 3:
                raise OSError('disk went away')
            return real(info, plan)

        monkeypatch.setattr(app, 'slice_pixels', flaky)
        out = tmp_path / 'out'
        with pytest.raises(OSError):
            repack(tmp_path / 'in', out / 'o.dcm')
        assert tree(out) == []

    def test_existing_output_is_only_replaced_on_success(self, tmp_path, monkeypatch):
        make_series(tmp_path / 'in', n=3)
        out = tmp_path / 'o.dcm'
        out.write_bytes(b'previous result')
        monkeypatch.setattr(app, 'slice_pixels', lambda *a: (_ for _ in ()).throw(OSError('x')))
        with pytest.raises(OSError):
            repack(tmp_path / 'in', out)
        assert out.read_bytes() == b'previous result'


# ---------------------------------------------------------------------- directories

class TestOutputPaths:

    def test_nested_series(self, tmp_path):
        i, o = tmp_path / 'in', tmp_path / 'out'
        assert app.output_path(i, o, i / 'study' / 'seriesA') == o / 'study' / 'seriesA.dcm'
        assert app.output_path(i, o, i / 'a' / 'b' / 'c') == o / 'a' / 'b' / 'c.dcm'
        assert app.output_path(i, o, i / 'series') == o / 'series.dcm'

    def test_flat_input_stays_inside_the_output_dir(self, tmp_path):
        # Regression: this used to be written NEXT TO the output dir ("outgoing.dcm"),
        # leaving outgoing/ empty while exiting 0.
        i, o = tmp_path / 'incoming', tmp_path / 'outgoing'
        dest = app.output_path(i, o, i)
        assert dest == o / 'incoming.dcm'
        assert o in dest.parents


class TestMain:

    def run(self, i, o, *argv):
        app.main(app.parser.parse_args(list(argv)), i, o)

    def test_nested_and_flat_series_and_no_stray_files(self, tmp_path):
        i, o = tmp_path / 'in', tmp_path / 'out'
        o.mkdir()
        e1 = make_series(i / 'study' / 'A', n=3)
        e2 = make_series(i / 'study' / 'B', n=4)
        e3 = make_series(i, n=2, rows=5)               # files directly in the input dir
        self.run(i, o)
        assert np.array_equal(read_out(o / 'study' / 'A.dcm')[1], e1)
        assert np.array_equal(read_out(o / 'study' / 'B.dcm')[1], e2)
        assert np.array_equal(read_out(o / 'in.dcm')[1], e3)
        assert not any(p.name.startswith('.') for p in o.rglob('*'))   # no .partial, no stray ".dcm" dir

    def test_one_bad_series_does_not_stop_the_others_and_exits_nonzero(self, tmp_path, capsys):
        i, o = tmp_path / 'in', tmp_path / 'out'
        o.mkdir()
        good = make_series(i / 'good', n=3)
        make_series(i / 'bad', n=2)
        make_slice(i / 'bad' / 'zz.dcm', np.zeros((9, 9), dtype=np.uint16))
        with pytest.raises(SystemExit) as exc:
            self.run(i, o)
        assert exc.value.code == 1
        assert np.array_equal(read_out(o / 'good.dcm')[1], good)
        assert not (o / 'bad.dcm').exists()
        assert 'not uniform' in capsys.readouterr().err

    def test_file_filter_option(self, tmp_path):
        i, o = tmp_path / 'in', tmp_path / 'out'
        o.mkdir()
        make_series(i / 's', n=2)
        for p in (i / 's').glob('*.dcm'):
            p.rename(p.with_suffix('.dicom'))
        self.run(i, o, '--fileFilter', 'dicom')
        assert (o / 's.dcm').exists()


# ------------------------------------------------------------------------ memory use

class TestMemory:
    """Peak Python-level allocation must not grow with the length of the series."""

    ROWS = COLS = 128                     # one 16-bit slice = 32 KiB
    N = 120                               # series = 3.75 MiB

    def peak_for(self, tmp_path, **kw):
        make_series(tmp_path / 'in', n=self.N, rows=self.ROWS, cols=self.COLS, **kw)
        tracemalloc.start()
        try:
            repack(tmp_path / 'in', tmp_path / 'o.dcm')
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_native_peak_is_a_few_slices_not_the_series(self, tmp_path):
        slice_bytes = self.ROWS * self.COLS * 2
        peak = self.peak_for(tmp_path)
        assert peak < 10 * slice_bytes, f'peak {peak} B for {self.N} slices of {slice_bytes} B'

    def test_compressed_peak_is_a_few_slices_not_the_series(self, tmp_path):
        slice_bytes = self.ROWS * self.COLS * 2
        peak = self.peak_for(tmp_path, rle=True)
        assert peak < 20 * slice_bytes, f'peak {peak} B for {self.N} slices of {slice_bytes} B'
