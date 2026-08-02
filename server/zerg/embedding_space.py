"""The one embedding space the corpus and every query share.

Query vectors and document vectors are only comparable when they came from the
same model, at the same dimension, with the same task framing. There is no valid
mixed state: a corpus half-embedded by one model is not a smaller corpus, it is
a corpus where similarity means two different things. So the identity lives in
one place and everything reads it from here.

Deliberately dependency-free. searchd is a separate lean process and must be
able to name the active space without importing model configuration, numpy
consumers, or tokenizers.
"""

from __future__ import annotations

from typing import Final

# EmbeddingGemma-300M, fp16 ONNX, truncated to 256 dimensions.
#
# Chosen by measurement, not by size. On 5,901 real episodes against the 89
# labelled queries it beat the 27x larger hosted Qwen3-8B at every k
# (recall@10 51.3% vs 39.5%) and improved every category. 768 dimensions
# measured *worse* at k=10 and k=25, so the extra width is not free accuracy —
# and 256 keeps the resident matrix at 85 MB.
ACTIVE_EMBEDDING_MODEL: Final = "google/embeddinggemma-300m"
ACTIVE_EMBEDDING_DIMS: Final = 256

# The previous space. Retained only so the migration can recognise and delete
# its rows; nothing may read from it.
LEGACY_EMBEDDING_MODEL: Final = "qwen/qwen3-embedding-8b"

# EmbeddingGemma is trained with an asymmetric retrieval framing. These strings
# are part of the space, not a formatting preference: embedding a query without
# its prefix lands it somewhere else entirely. Measured worth ~10 points of
# recall, enough that omitting them made the better model look like the worse
# one.
QUERY_PREFIX: Final = "task: search result | query: "
DOCUMENT_PREFIX: Final = "title: none | text: "
