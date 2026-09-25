from senda_argus_hooks.core.hashing import derive_embedding_sketch


def test_sketch_reduces_dimension():
    vector = [float(i) for i in range(768)]
    sketch = derive_embedding_sketch(vector, dim_out=16)
    assert len(sketch) == 16


def test_sketch_is_deterministic():
    vector = [0.1, 0.2, 0.3] * 100
    a = derive_embedding_sketch(vector, dim_out=16)
    b = derive_embedding_sketch(vector, dim_out=16)
    assert a == b


def test_similar_vectors_produce_similar_sketches():
    base = [float(i % 7) for i in range(300)]
    near = [v + 0.001 for v in base]
    far = [float((i + 1) % 5) for i in range(300)]

    sketch_base = derive_embedding_sketch(base, dim_out=16)
    sketch_near = derive_embedding_sketch(near, dim_out=16)
    sketch_far = derive_embedding_sketch(far, dim_out=16)

    def _dist(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5

    assert _dist(sketch_base, sketch_near) < _dist(sketch_base, sketch_far)


def test_small_vector_returned_unchanged_when_below_dim_out():
    vector = [1.0, 2.0, 3.0]
    assert derive_embedding_sketch(vector, dim_out=16) == vector


def test_non_list_returns_none():
    assert derive_embedding_sketch(None) is None
    assert derive_embedding_sketch("not a vector") is None


def test_non_numeric_elements_returns_none():
    assert derive_embedding_sketch(["a", "b", "c"]) is None
