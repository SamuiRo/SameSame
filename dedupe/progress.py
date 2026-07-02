from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable, Iterator, TypeVar

from .events import CancellationToken, ScanEvent, ScanEventCallback, ScanEventType, ScanStage

T = TypeVar("T")

try:
    from tqdm import tqdm as _tqdm
except ImportError:
    _tqdm = None


@dataclass(frozen=True, slots=True)
class _ProgressContext:
    stage: ScanStage
    callback: ScanEventCallback | None
    cancellation: CancellationToken
    show_terminal: bool


_CONTEXT: ContextVar[_ProgressContext | None] = ContextVar("samesame_progress_context", default=None)


@contextmanager
def progress_scope(
    *,
    stage: ScanStage,
    callback: ScanEventCallback | None,
    cancellation: CancellationToken,
    show_terminal: bool,
) -> Iterator[None]:
    token = _CONTEXT.set(_ProgressContext(stage, callback, cancellation, show_terminal))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def check_cancelled() -> None:
    context = _CONTEXT.get()
    if context is not None:
        context.cancellation.raise_if_cancelled()


def tqdm(iterable: Iterable[T], *args: object, **kwargs: object) -> Iterator[T]:
    context = _CONTEXT.get()
    if context is None and _tqdm is None:
        yield from iterable
        return

    progress_kwargs = dict(kwargs)
    if context is not None and not context.show_terminal:
        # Constructing even a disabled tqdm lazily creates a multiprocessing
        # lock. On Windows that import/lock initialization can crash when a Qt
        # scan first reaches it from a background QThread. GUI clients already
        # receive structured progress events, so no tqdm object is needed.
        wrapped = iterable
    else:
        wrapped = _tqdm(iterable, *args, **progress_kwargs) if _tqdm is not None else iterable
    description = str(progress_kwargs.get("desc") or "")
    unit = str(progress_kwargs.get("unit") or "item")
    total_value = progress_kwargs.get("total")
    if total_value is None:
        try:
            total_value = len(iterable)  # type: ignore[arg-type]
        except TypeError:
            total_value = None
    total = int(total_value) if isinstance(total_value, (int, float)) else None

    for current, item in enumerate(wrapped, start=1):
        if context is not None:
            context.cancellation.raise_if_cancelled()
        yield item
        if context is not None and context.callback is not None:
            context.callback(
                ScanEvent(
                    ScanEventType.PROGRESS,
                    stage=context.stage,
                    message=description,
                    current=current,
                    total=total,
                    unit=unit,
                )
            )
    if context is not None:
        context.cancellation.raise_if_cancelled()
