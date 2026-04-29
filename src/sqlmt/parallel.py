"""Small ordered concurrency helpers shared across pipeline stages."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Generic, TypeVar

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class _Completed(Generic[ResultT]):
    """One completed worker result or exception."""

    value: ResultT | None = None
    error: BaseException | None = None


def ordered_parallel_map(
    items: Sequence[ItemT],
    *,
    workers: int,
    fn: Callable[[ItemT], ResultT],
) -> Iterator[ResultT]:
    """Yield results in input order while running up to ``workers`` tasks at once."""

    if workers <= 1:
        for item in items:
            yield fn(item)
        return

    if not items:
        return

    executor = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    futures: dict[Future[ResultT], int] = {}
    buffered: dict[int, _Completed[ResultT]] = {}
    next_submit = 0
    next_yield = 0

    def submit_until_full() -> None:
        nonlocal next_submit
        while len(futures) < workers and next_submit < len(items):
            future = executor.submit(fn, items[next_submit])
            futures[future] = next_submit
            next_submit += 1

    submit_until_full()
    try:
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                index = futures.pop(future)
                try:
                    value = future.result()
                except BaseException as exc:  # noqa: B036
                    buffered[index] = _Completed(error=exc)
                else:
                    buffered[index] = _Completed(value=value)
            submit_until_full()
            while next_yield in buffered:
                completed = buffered.pop(next_yield)
                next_yield += 1
                if completed.error is not None:
                    raise completed.error
                yield completed.value
                submit_until_full()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
