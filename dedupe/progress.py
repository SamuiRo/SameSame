from __future__ import annotations

from typing import Iterable, Iterator, TypeVar

T = TypeVar("T")

try:
    from tqdm import tqdm as _tqdm
except ImportError:
    _tqdm = None


def tqdm(iterable: Iterable[T], *args: object, **kwargs: object) -> Iterator[T]:
    if _tqdm is None:
        yield from iterable
    else:
        yield from _tqdm(iterable, *args, **kwargs)
