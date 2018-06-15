"""
Test date sequence generation functions as used by statistics apps


"""
import os
import string

from types import SimpleNamespace
import pytest
import rasterio
from dateutil.parser import parse
from hypothesis import given
from hypothesis.strategies import integers, text
from pandas import to_datetime
import pathlib
import xarray as xr
import numpy as np

from datacube.helpers import write_geotiff
from datacube.utils import uri_to_local_path, clamp, gen_password, write_user_secret_file, slurp
from datacube.utils import without_lineage_sources, map_with_lookahead, read_documents
from datacube.utils import mk_part_uri, get_part_from_uri
from datacube.utils.changes import check_doc_unchanged, get_doc_changes, MISSING, DocumentMismatchError
from datacube.utils.dates import date_sequence
from datacube.model.utils import xr_apply, traverse_datasets
from datacube.model import MetadataType

from .util import mk_sample_product


def test_stats_dates():
    # Winter for 1990
    winter_1990 = list(date_sequence(start=to_datetime('1990-06-01'), end=to_datetime('1990-09-01'), step_size='3m',
                                     stats_duration='3m'))
    assert winter_1990 == [(parse('1990-06-01'), parse('1990-09-01'))]

    # Every winter from 1990 - 1992
    three_years_of_winter = list(date_sequence(start=to_datetime('1990-06-01'), end=to_datetime('1992-09-01'),
                                               step_size='1y',
                                               stats_duration='3m'))
    assert three_years_of_winter == [(parse('1990-06-01'), parse('1990-09-01')),
                                     (parse('1991-06-01'), parse('1991-09-01')),
                                     (parse('1992-06-01'), parse('1992-09-01'))]

    # Full years from 1990 - 1994
    five_full_years = list(date_sequence(start=to_datetime('1990-01-01'), end=to_datetime('1995'), step_size='1y',
                                         stats_duration='1y'))
    assert five_full_years == [(parse('1990-01-01'), parse('1991-01-01')),
                               (parse('1991-01-01'), parse('1992-01-01')),
                               (parse('1992-01-01'), parse('1993-01-01')),
                               (parse('1993-01-01'), parse('1994-01-01')),
                               (parse('1994-01-01'), parse('1995-01-01'))]

    # Every season (three months), starting in March, from 1990 until end 1992-02
    two_years_of_seasons = list(date_sequence(start=to_datetime('1990-03-01'), end=to_datetime('1992-03'),
                                              step_size='3m',
                                              stats_duration='3m'))
    assert len(two_years_of_seasons) == 8
    assert two_years_of_seasons == [(parse('1990-03-01'), parse('1990-06-01')),
                                    (parse('1990-06-01'), parse('1990-09-01')),
                                    (parse('1990-09-01'), parse('1990-12-01')),
                                    (parse('1990-12-01'), parse('1991-03-01')),
                                    (parse('1991-03-01'), parse('1991-06-01')),
                                    (parse('1991-06-01'), parse('1991-09-01')),
                                    (parse('1991-09-01'), parse('1991-12-01')),
                                    (parse('1991-12-01'), parse('1992-03-01'))]  # Leap year!

    # Every month from 1990-01 to 1990-06
    monthly = list(date_sequence(start=to_datetime('1990-01-01'), end=to_datetime('1990-07-01'), step_size='1m',
                                 stats_duration='1m'))
    assert len(monthly) == 6

    # Complex
    # I want the average over 5 years


def test_uri_to_local_path():
    if os.name == 'nt':
        assert 'C:\\tmp\\test.tmp' == str(uri_to_local_path('file:///C:/tmp/test.tmp'))

    else:
        assert '/tmp/something.txt' == str(uri_to_local_path('file:///tmp/something.txt'))

    assert uri_to_local_path(None) is None

    with pytest.raises(ValueError):
        uri_to_local_path('ftp://example.com/tmp/something.txt')


@given(integers(), integers(), integers())
def test_clamp(x, lower_bound, upper_bound):
    if lower_bound > upper_bound:
        lower_bound, upper_bound = upper_bound, lower_bound
    new_x = clamp(x, lower_bound, upper_bound)

    # If x was already between the bounds, it shouldn't have changed
    if lower_bound <= x <= upper_bound:
        assert new_x == x
    assert lower_bound <= new_x <= upper_bound


@given(integers(min_value=10, max_value=30))
def test_gen_pass(n_bytes):
    password1 = gen_password(n_bytes)
    password2 = gen_password(n_bytes)
    assert len(password1) >= n_bytes
    assert len(password2) >= n_bytes
    assert password1 != password2


@given(text(alphabet=string.digits + string.ascii_letters + ' ,:.![]?', max_size=20))
def test_write_user_secret_file(txt):
    fname = u".tst-datacube-uefvwr4cfkkl0ijk.txt"

    write_user_secret_file(txt, fname)
    txt_back = slurp(fname)
    os.remove(fname)
    assert txt == txt_back
    assert slurp(fname) is None


doc_changes = [
    (1, 1, []),
    ({}, {}, []),
    ({'a': 1}, {'a': 1}, []),
    ({'a': {'b': 1}}, {'a': {'b': 1}}, []),
    ([1, 2, 3], [1, 2, 3], []),
    ([1, 2, [3, 4, 5]], [1, 2, [3, 4, 5]], []),
    (1, 2, [((), 1, 2)]),
    ([1, 2, 3], [2, 1, 3], [((0,), 1, 2), ((1,), 2, 1)]),
    ([1, 2, [3, 4, 5]], [1, 2, [3, 6, 7]], [((2, 1), 4, 6), ((2, 2), 5, 7)]),
    ({'a': 1}, {'a': 2}, [(('a',), 1, 2)]),
    ({'a': 1}, {'a': 2}, [(('a',), 1, 2)]),
    ({'a': 1}, {'b': 1}, [(('a',), 1, MISSING), (('b',), MISSING, 1)]),
    ({'a': {'b': 1}}, {'a': {'b': 2}}, [(('a', 'b'), 1, 2)]),
    ({}, {'b': 1}, [(('b',), MISSING, 1)]),
    ({'a': {'c': 1}}, {'a': {'b': 1}}, [(('a', 'b'), MISSING, 1), (('a', 'c'), 1, MISSING)])
]


@pytest.mark.parametrize("v1, v2, expected", doc_changes)
def test_get_doc_changes(v1, v2, expected):
    rval = get_doc_changes(v1, v2)
    assert rval == expected


def test_get_doc_changes_w_baseprefix():
    rval = get_doc_changes({}, None, base_prefix=('a',))
    assert rval == [(('a',), {}, None)]


@pytest.mark.parametrize("v1, v2, expected", doc_changes)
def test_check_doc_unchanged(v1, v2, expected):
    if expected != []:
        with pytest.raises(DocumentMismatchError):
            rval = check_doc_unchanged(v1, v2, 'name')
    else:
        # No Error Raised
        check_doc_unchanged(v1, v2, 'name')


def test_more_check_doc_unchanged():
    # No exception raised
    check_doc_unchanged({'a': 1}, {'a': 1}, 'Letters')

    with pytest.raises(DocumentMismatchError, message='Letters differs from stored (a: 1!=2)'):
        check_doc_unchanged({'a': 1}, {'a': 2}, 'Letters')

    with pytest.raises(DocumentMismatchError, message='Letters differs from stored (a.b: 1!=2)'):
        check_doc_unchanged({'a': {'b': 1}}, {'a': {'b': 2}}, 'Letters')


def test_write_geotiff(tmpdir, odc_style_xr_dataset):
    """Ensure the geotiff helper writer works, and supports datasets smaller than 256x256."""
    filename = tmpdir + '/test.tif'

    assert len(odc_style_xr_dataset.latitude) < 256

    write_geotiff(filename, odc_style_xr_dataset)

    assert filename.exists()

    with rasterio.open(str(filename)) as src:
        written_data = src.read(1)

        assert (written_data == odc_style_xr_dataset['B10']).all()


def test_write_geotiff_time_index_deprecated():
    """The `time_index` parameter to `write_geotiff()` was a poorly thought out addition and is now deprecated."""

    with pytest.raises(ValueError):
        write_geotiff("", None, time_index=1)


def test_without_lineage_sources():
    def mk_sample(v):
        return dict(lineage={'source_datasets': v, 'a': 'a', 'b': 'b'},
                    aa='aa',
                    bb=dict(bb='bb'))

    spec = mk_sample_product('tt')

    x = {'a': 1}
    assert without_lineage_sources(x, spec) == x
    assert without_lineage_sources(x, spec, inplace=True) == x

    x = {'a': 1, 'lineage': {}}
    assert without_lineage_sources(x, spec) == x
    assert without_lineage_sources(x, spec, inplace=True) == x

    x = mk_sample(1)
    assert without_lineage_sources(x, spec) != x
    assert x['lineage']['source_datasets'] == 1

    x = mk_sample(2)
    assert without_lineage_sources(x, spec, inplace=True) == x
    assert x['lineage']['source_datasets'] == {}

    assert mk_sample(10) != mk_sample({})
    assert without_lineage_sources(mk_sample(10), spec) == mk_sample({})
    assert without_lineage_sources(mk_sample(10), spec, inplace=True) == mk_sample({})

    # check behaviour when `sources` is not defined for the type
    no_sources_type = MetadataType({
        'name': 'eo',
        'description': 'Sample',
        'dataset': dict(
            id=['id'],
            label=['ga_label'],
            creation_time=['creation_dt'],
            measurements=['image', 'bands'],
            format=['format', 'name'],
        )
    }, dataset_search_fields={})

    assert without_lineage_sources(mk_sample(10), no_sources_type) == mk_sample(10)
    assert without_lineage_sources(mk_sample(10), no_sources_type, inplace=True) == mk_sample(10)


def test_map_with_lookahead():
    def if_one(x):
        return 'one'+str(x)

    def if_many(x):
        return 'many'+str(x)

    assert list(map_with_lookahead(iter([]), if_one, if_many)) == []
    assert list(map_with_lookahead(iter([1]), if_one, if_many)) == [if_one(1)]
    assert list(map_with_lookahead(range(5), if_one, if_many)) == list(map(if_many, range(5)))
    assert list(map_with_lookahead(range(10), if_one=if_one)) == list(range(10))
    assert list(map_with_lookahead(iter([1]), if_many=if_many)) == [1]


def test_part_uri():
    base = 'file:///foo.txt'

    for i in range(10):
        assert get_part_from_uri(mk_part_uri(base, i)) == i

    assert get_part_from_uri('file:///f.txt') is None
    assert get_part_from_uri('file:///f.txt#something_else') is None
    assert get_part_from_uri('file:///f.txt#part=aa') == 'aa'
    assert get_part_from_uri('file:///f.txt#part=111') == 111


def test_read_documents(sample_document_files):
    for filename, ndocs in sample_document_files:
        all_docs = list(read_documents(filename))
        assert len(all_docs) == ndocs

        for path, doc in all_docs:
            assert isinstance(doc, dict)
            assert isinstance(path, pathlib.Path)

        assert set(str(f) for f, _ in all_docs) == set([filename])

    for filename, ndocs in sample_document_files:
        all_docs = list(read_documents(filename, uri=True))
        assert len(all_docs) == ndocs

        for uri, doc in all_docs:
            assert isinstance(doc, dict)
            assert isinstance(uri, str)

        p = pathlib.Path(filename)
        if ndocs > 1:
            expect_uris = [p.as_uri() + '#part={}'.format(i) for i in range(ndocs)]
        else:
            expect_uris = [p.as_uri()]

        assert [f for f, _ in all_docs] == expect_uris


def test_xr_apply():
    src = xr.DataArray(np.asarray([1, 2, 3], dtype='uint8'), dims=['time'])
    dst = xr_apply(src, lambda _, v: v, dtype='float32')

    assert dst.dtype.name == 'float32'
    assert dst.shape == src.shape
    assert dst.values.tolist() == [1, 2, 3]

    dst = xr_apply(src, lambda _, v: v)
    assert dst.dtype.name == 'uint8'
    assert dst.shape == src.shape
    assert dst.values.tolist() == [1, 2, 3]

    dst = xr_apply(src, lambda idx, _, v: idx[0] + v, with_numeric_index=True)
    assert dst.dtype.name == 'uint8'
    assert dst.shape == src.shape
    assert dst.values.tolist() == [0+1, 1+2, 2+3]


def test_traverse_datasets():
    """
      A -> B
      |    |
      |    v
      +--> C -> D
      |
      +--> E
    """
    def node(name, **kwargs):
        return SimpleNamespace(id=name, sources=kwargs)

    D = node('D')
    E = node('E')
    C = node('C', cd=D)
    B = node('B', bc=C)
    A = node('A', ab=B, ac=C, ae=E)

    def visitor(node, name=None, depth=0, out=None):
        s = '{}:{}:{:d}'.format(node.id, name if name else '..', depth)
        out.append(s)

    with pytest.raises(ValueError):
        traverse_datasets(A, visitor, mode='not-a-real-mode')

    expect_preorder = '''
A:..:0
B:ab:1
C:bc:2
D:cd:3
C:ac:1
D:cd:2
E:ae:1
'''.lstrip().rstrip()

    expect_postorder = '''
D:cd:3
C:bc:2
B:ab:1
D:cd:2
C:ac:1
E:ae:1
A:..:0
'''.lstrip().rstrip()

    for mode, expect in zip(['pre-order', 'post-order'],
                            [expect_preorder, expect_postorder]):
        out = []
        traverse_datasets(A, visitor, mode=mode, out=out)
        assert '\n'.join(out) == expect
