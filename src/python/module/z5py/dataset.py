import os
import numbers
import json

import numpy as np
from ._z5py import open_dataset
from ._z5py import write_subarray, write_scalar, read_subarray, convert_array_to_format
from .attribute_manager import AttributeManager


def slice_to_begin_shape(s, size):
    if s.step not in (None, 1):
        raise ValueError('Nontrivial steps are not supported')

    if s.start is None:
        begin = 0
    elif -size <= s.start < 0:
        begin = size + s.start
    elif s.start < -size or s.start >= size:
        return None, 0
    else:
        begin = s.start

    if s.stop is None or s.stop > size:
        shape = size - begin
    elif s.stop < 0:
        shape = (size + s.stop) - begin
    else:
        shape = s.stop - begin

    if shape < 1:
        return None, 0

    return begin, shape


def int_to_begin_shape(i, size):
    if -size < i < 0:
        begin = i + size
    elif i >= size or i < -size:
        raise ValueError('Index ({}) out of range (0-{})'.format(i, size-1))
    else:
        begin = i

    return begin, 1


class Dataset(object):

    dtype_dict = {np.dtype('uint8'): 'uint8',
                  np.dtype('uint16'): 'uint16',
                  np.dtype('uint32'): 'uint32',
                  np.dtype('uint64'): 'uint64',
                  np.dtype('int8'): 'int8',
                  np.dtype('int16'): 'int16',
                  np.dtype('int32'): 'int32',
                  np.dtype('int64'): 'int64',
                  np.dtype('float32'): 'float32',
                  np.dtype('float64'): 'float64'}

    zarr_dtype_dict = {np.dtype('uint8'): '<u1',
                       np.dtype('uint16'): '<u2',
                       np.dtype('uint32'): '<u4',
                       np.dtype('uint64'): '<u8',
                       np.dtype('int8'): '<i1',
                       np.dtype('int16'): '<i2',
                       np.dtype('int32'): '<i4',
                       np.dtype('int64'): '<i8',
                       np.dtype('float32'): '<f4',
                       np.dtype('float64'): '<f8'}

    # FIXME for now we hardcode all compressors
    # but we should instead check which ones are present
    # (similar to nifty WITH_CPLEX, etc.)
    compressors_zarr = ['raw', 'blosc', 'zlib', 'bzip2']
    compressors_n5 = ['raw', 'gzip', 'bzip2']
    zarr_default_compressor = 'blosc'
    n5_default_compressor = 'gzip'

    def __init__(self, path, dset_impl):
        self._impl = dset_impl
        self._attrs = AttributeManager(path, self._impl.is_zarr)
        self.path = path

    @staticmethod
    def _to_zarr_compression_options(compression, compression_options):
        opts = {}
        if compression == 'blosc':
            opts['id'] = 'blosc'
            opts['cname'] = compression_options.get('codec', 'lz4')
            opts['clevel'] = compression_options.get('level', 5)
            opts['shuffle'] = compression_options.get('shuffle', 1)
        elif compression == 'zlib':
            opts['id'] = 'zlib'
            opts['level'] = compression_options.get('level', 5)
        elif compression == 'bzip2':
            opts['id'] = 'bzip2'
            opts['level'] = compression_options.get('level', 5)
        elif compression == 'raw':
            opts = None
        else:
            raise RuntimeError("Compression %s is not supported in zarr format" % compression)
        return opts

    def _read_zarr_compression_options(self):
        opts = {}
        with open(os.path.join(self.path, '.zarray'), 'r') as f:
            zarr_opts = json.load(f)
        if zarr_opts is None:
            opts['compression'] = 'raw'
        elif zarr_opts['id'] == 'blosc':
            opts['compression'] = 'blosc'
            opts['level'] = zarr_opts['clevel']
            opts['shuffle'] = zarr_opts['shuffle']
            opts['codec'] = zarr_opts['cname']
        elif zarr_opts['id'] == 'zlib':
            opts['compression'] = 'zlib'
            opts['level'] = zarr_opts['level']
        elif zarr_opts['id'] == 'bzip2':
            opts['compression'] = 'bzip2'
            opts['level'] = zarr_opts['level']
        return opts

    @staticmethod
    def _to_n5_compression_options(compression, compression_options):
        opts = {}
        # TODO blosc in n5
        # if compression == 'blosc':
        #     opts['type'] = 'blosc'
        #     opts['codec'] = compression_options['codec']
        #     opts['level'] = compression_options['level']
        #     opts['shuffle'] = compression_options['shuffle']
        if compression == 'gzip':
            opts['type'] = 'gzip'
            opts['level'] = compression_options.get('level', 5)
        elif compression == 'bzip2':
            opts['type'] = 'bzip2'
            opts['blockSize'] = compression_options.get('level', 5)
        elif compression == 'raw':
            opts['type'] = 'raw'
        else:
            raise RuntimeError("Compression %s is not supported in n5 format" % compression)
        return opts

    def _read_n5_compression_options(self):
        opts = {}
        with open(os.path.join(self.path, 'attributes.json'), 'r') as f:
            n5_opts = json.load(f)
        # old compression scheme
        if 'compressionType' in n5_opts:
            ctype = n5_opts['compressionType']
            new_compression = False
        # new compression scheme
        else:
            ctype = n5_opts['compression']['type']
            new_compression = True

        if ctype == 'raw':
            opts['compression'] = 'raw'
        elif ctype == 'gzip':
            opts['compression'] = 'gzip'
            opts['level'] = n5_opts['compression']['level'] if new_compression else 5
        elif ctype['compression'] == 'bzip2':
            opts['compression'] = 'bzip2'
            opts['level'] = n5_opts['compression']['blockSize'] if new_compression else 5
        # TODO blosc in n5
        # elif n5_opts['id'] == 'blosc':
        #     opts['compression'] = 'blosc'
        #     opts['level'] = n5_opts['clevel']
        #     opts['shuffle'] = n5_opts['shuffle']
        #     opts['codec'] = n5_opts['cname']
        return opts

    @staticmethod
    def _create_dataset_zarr(path, dtype, shape, chunks,
                             compression, compression_options,
                             fill_value):
        os.mkdir(path)
        params = {'dtype': Dataset.zarr_dtype_dict[np.dtype(dtype)],
                  'shape': shape,
                  'chunks': chunks,
                  'fill_value': fill_value,
                  'compressor': Dataset._to_zarr_compression_options(compression,
                                                                     compression_options)}
        with open(os.path.join(path, '.zarray'), 'w') as f:
            json.dump(params, f)

    @staticmethod
    def _create_dataset_n5(path, dtype, shape, chunks,
                           compression, compression_options):
        os.mkdir(path)
        params = {'dataType': Dataset.dtype_dict[np.dtype(dtype)],
                  'dimensions': shape[::-1],
                  'blockSize': chunks[::-1],
                  'compression': Dataset._to_n5_compression_options(compression,
                                                                    compression_options)}
        with open(os.path.join(path, 'attributes.json'), 'w') as f:
            json.dump(params, f)

    @classmethod
    def create_dataset(cls, path, dtype,
                       shape, chunks, is_zarr,
                       compression, compression_options,
                       fill_value, mode):
        if os.path.exists(path):
            raise RuntimeError("Cannot create existing dataset")
        if is_zarr and compression not in cls.compressors_zarr:
            compression = cls.zarr_default_compressor
        elif not is_zarr and compression not in cls.compressors_n5:
            compression = cls.n5_default_compressor

        parsed_dtype = np.dtype(dtype)

        if is_zarr:
            if parsed_dtype not in cls.zarr_dtype_dict:
                raise ValueError("Invalid data type {} for zarr dataset".format(dtype))
            cls._create_dataset_zarr(path, parsed_dtype, shape, chunks,
                                     compression, compression_options, fill_value)
        else:
            if parsed_dtype not in cls.dtype_dict:
                raise ValueError("Invalid data type {} for N5 dataset".format(repr(dtype)))
            cls._create_dataset_n5(path, parsed_dtype, shape, chunks,
                                   compression, compression_options)
        return cls(path, open_dataset(path, mode))

    @classmethod
    def open_dataset(cls, path, mode):
        return cls(path, open_dataset(path, mode))

    @property
    def is_zarr(self):
        return self._impl.is_zarr

    @property
    def attrs(self):
        return self._attrs

    @property
    def shape(self):
        return tuple(self._impl.shape)

    @property
    def ndim(self):
        return self._impl.ndim

    @property
    def size(self):
        return self._impl.size

    @property
    def chunks(self):
        return tuple(self._impl.chunks)

    @property
    def dtype(self):
        return np.dtype(self._impl.dtype)

    @property
    def chunks_per_dimension(self):
        return self._impl.chunks_per_dimension

    @property
    def number_of_chunks(self):
        return self._impl.number_of_chunks

    @property
    def compression_options(self):
        return self._read_zarr_compression_options() if self._impl.is_zarr else \
            self._read_n5_compression_options()

    def __len__(self):
        return self._impl.len

    def index_to_roi(self, index):
        type_msg = 'Advanced selection inappropriate. ' \
                   'Only numbers, slices (`:`), and ellipsis (`...`) are valid indices (or tuples thereof)'

        if isinstance(index, tuple):
            index_lst = list(index)
        elif isinstance(index, (numbers.Number, slice, type(Ellipsis))):
            index_lst = [index]
        else:
            raise TypeError(type_msg)

        if len([item for item in index_lst if item != Ellipsis]) > self.ndim:
            raise TypeError("Argument sequence too long")
        elif len(index_lst) < self.ndim and Ellipsis not in index_lst:
            index_lst.append(Ellipsis)

        start_shapes = []
        found_ellipsis = False
        for item in index_lst:
            d = len(start_shapes)
            if isinstance(item, slice):
                start_shapes.append(slice_to_begin_shape(item, self.shape[d]))
            elif isinstance(item, numbers.Number):
                start_shapes.append(int_to_begin_shape(int(item), self.shape[d]))
            elif isinstance(item, type(Ellipsis)):
                if found_ellipsis:
                    raise ValueError("Only one ellipsis may be used")
                found_ellipsis = True
                while len(start_shapes) + (len(index_lst) - d - 1) < self.ndim:
                    start_shapes.append((0, self.shape[len(start_shapes)]))
            else:
                raise TypeError(type_msg)

        roi_begin, roi_shape = zip(*start_shapes)
        return roi_begin, roi_shape

    # most checks are done in c++
    def __getitem__(self, index):
        # todo: support newaxis, integer/boolean arrays, striding
        roi_begin, shape = self.index_to_roi(index)
        out = np.empty(shape, dtype=self.dtype)
        if 0 in shape:
            return out
        read_subarray(self._impl, out, roi_begin)
        try:
            if len(index) == self.ndim and all(isinstance(i, numbers.Number) for i in index):
                return out[(0,) * out.ndim]
        except TypeError:
            pass
        return out

    # most checks are done in c++
    def __setitem__(self, index, item):
        if not isinstance(item, (numbers.Number, np.ndarray)):
            raise ValueError("Invalid item")
        roi_begin, shape = self.index_to_roi(index)
        if 0 in shape:
            return

        # n5 input must be transpsed due to different axis convention
        # write the complete array
        if isinstance(item, np.ndarray):
            if item.ndim != self.ndim:
                raise ValueError("Complicated broadcasting is not supported")
            write_subarray(self._impl, np.require(item, requirements='C'), roi_begin)

        # broadcast scalar
        else:
            # FIXME this seems to be broken; fails with RuntimeError('WrongRequest Shape')
            write_scalar(self._impl, roi_begin, list(shape), item)

    def find_minimum_coordinates(self, dim):
        return self._impl.findMinimumCoordinates(dim)

    def find_maximum_coordinates(self, dim):
        return self._impl.findMaximumCoordinates(dim)

    # expose the impl write subarray functionality
    def write_subarray(self, start, data):
        write_subarray(self._impl, np.require(data, requirements='C'), start)

    # expose the impl read subarray functionality
    def read_subarray(self, start, stop):
        shape = tuple(sto - sta for sta, sto in zip(start, stop))
        out = np.empty(shape, dtype=self.dtype)
        read_subarray(self._impl, out, start)
        return out

    def array_to_format(self, array):
        if array.ndim != self.ndim:
            raise RuntimeError("Array needs to be of same dimension as dataset")
        if np.dtype(array.dtype) != np.dtype(self.dtype):
            raise RuntimeError("Array needs to have same dtype as dataset")
        return convert_array_to_format(self._impl, np.require(array, requirements='C'))

    def chunk_exists(self, chunk_indices):
        return self._impl.chunkExists(chunk_indices)
