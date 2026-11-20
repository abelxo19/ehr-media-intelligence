"""EHR Media Intelligence Platform."""

import os

# Cap BLAS/OpenMP thread pools before numpy/torch are imported. On small machines OpenBLAS
# allocates a buffer per thread and can fail with "Memory allocation still failed"; at this
# data size single-digit threads are plenty. Users can still override via the environment.
for _var, _value in (("OPENBLAS_NUM_THREADS", "1"), ("OMP_NUM_THREADS", "2"), ("MKL_NUM_THREADS", "2"),
                     ("TOKENIZERS_PARALLELISM", "false")):
    os.environ.setdefault(_var, _value)
