#!/usr/bin/env python

from pathlib import Path
from argparse import ArgumentParser, Namespace, ArgumentDefaultsHelpFormatter

import numpy as np
from chris_plugin import chris_plugin, PathMapper
import pydicom as dicom
import os
from pflog import pflog
__version__ = '1.2.2'

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
parser.add_argument(  '--pftelDB',
                    dest        = 'pftelDB',
                    default     = '',
                    type        = str,
                    help        = 'optional pftel server DB path')

# The main function of this *ChRIS* plugin is denoted by this ``@chris_plugin`` "decorator."
# Some metadata about the plugin is specified here. There is more metadata specified in setup.py.
#
# documentation: https://fnndsc.github.io/chris_plugin/chris_plugin.html#chris_plugin
@chris_plugin(
    parser=parser,
    title='A DICOM repack plugin',
    category='',                 # ref. https://chrisstore.co/plugins
    min_memory_limit='8Gi',    # supported units: Mi, Gi
    min_cpu_limit='4000m',       # millicores, e.g. "1000m" = 1 CPU core
    min_gpu_limit=0              # set min_gpu_limit=1 to enable GPU
)
@pflog.tel_logTime(
            event       = 'dicom_repack',
            log         = 'Repack slices of dicoms into one'
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

    # Typically it's easier to think of programs as operating on individual files
    # rather than directories. The helper functions provided by a ``PathMapper``
    # object make it easy to discover input files and write to output files inside
    # the given paths.
    #
    # Refer to the documentation for more options, examples, and advanced uses e.g.
    # adding a progress bar and parallelism.
    mapper = PathMapper.file_mapper(inputdir, outputdir, glob=f"**/*.{options.fileFilter}", fail_if_empty=False)
    file_sets = {} # Contains all unique dirs along with the file list
    for input_file, output_file in mapper:
        input_file_dir = str(input_file).replace(input_file.name,'')
        if input_file_dir not in file_sets.keys():
            file_sets[input_file_dir]=[input_file.name]
        else:
            file_sets[input_file_dir].append(input_file.name)
    for dicom_file_set in file_sets.keys():
        merge_dicom = merge_dicom_multiframe(dicom_file_set, file_sets[dicom_file_set])
        op_path = dicom_file_set.replace(str(inputdir),str(outputdir))
        op_dicom_filename = os.path.basename(os.path.normpath(op_path))
        op_dicom_filepath = os.path.dirname(op_path) + ".dcm"
        op_dicom_dir = op_dicom_filepath.replace(op_dicom_filename, '')
        os.makedirs(op_dicom_dir,exist_ok=True)
        print(f"Saving output file: ---->{op_dicom_filename}.dcm<----")
        merge_dicom.save_as(op_dicom_filepath)


if __name__ == '__main__':
    main()


def read_dicom(dicom_path):
    dataset = None
    try:
        dataset = dicom.dcmread(dicom_path)
    except Exception as ex:
        print(dicom_path, ex)
    return dataset

def merge_dicom_multiframe(dir_name, dicom_list):
    slices = len(dicom_list)
    print(f"Incoming directory location: --->{dir_name}<---")
    op_dicom = read_dicom(os.path.join(dir_name, dicom_list[0]))
    _Vnp_3DVol = [] #np.zeros((slices, shape3D[0], shape3D[1],shape3D[2] ))
    i = 0
    for img in sorted(dicom_list):
        dicom_path = os.path.join(dir_name, img)
        print(f"Reading dicom file: --->{img}<---")
        dcm = read_dicom(dicom_path)
        image = dcm.pixel_array
        try:
            _Vnp_3DVol.append(image)
        except Exception as e:
            print(e)
        i += 1
    op_dicom.NumberOfFrames = slices
    op_dicom.PixelData = np.array(_Vnp_3DVol).tobytes()

    return op_dicom