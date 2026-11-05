"""InSAR pipeline: ISCE2 topsStack coregistration -> dolphin phase linking -> MintPy.

Each processing stage lives in its own module:

- ``isce_stack``     : run ISCE2 topsStack (stackSentinel.py + run files)
- ``radar_crop``     : cut the coregistered radar-coordinate stack to the AOI
- ``dolphin_step``   : write the dolphin config and run phase linking/unwrapping
- ``mintpy_inputs``  : convert dolphin outputs + ISCE2 geometry to MintPy HDF5 files
- ``mintpy_step``    : run MintPy network inversion and/or corrections
"""

__version__ = "0.1.0"
