import itertools
import math
from random import shuffle
from typing import Iterator, List, Tuple

import torch
from omegaconf import DictConfig
from torch.utils.data import IterableDataset, get_worker_info

from src.psi_datapoint.psi_datapoint_facade import PSIDatapointFacade


class PSIDataset(IterableDataset):
    def __init__(
        self,
        config: DictConfig,
        holdout: str,
        rank: int,
        world_size: int,
    ):
        assert 0 <= rank < world_size
        self._rank = rank
        self._world_size = world_size

        if holdout == "train":
            self._data_path = config.source_data.train_jsonl
            self._need_shuffle = True
        elif holdout == "val":
            self._data_path = config.source_data.val_jsonl
            self._need_shuffle = False
        elif holdout == "test":
            self._data_path = config.source_data.test_jsonl
            self._need_shuffle = False
        else:
            raise ValueError(f"Invalid holdout value {holdout}")

        self._psi_facade = PSIDatapointFacade(config)
        self._shuffle_bucket = config.dataset.shuffle_bucket
        self._overlap_slicing = config.dataset.overlap_slicing
        self._pad_overlapped = config.dataset.pad_overlapped
        assert 0.0 <= self._overlap_slicing < 1.0
        self._example_len = config.model.context_length
        self._labels_pad = config.model.labels_pad

    def __len__(self) -> int:
        return (
            sum(
                int(math.ceil(size / ((1 - self._overlap_slicing) * self._example_len)))
                for size in self._psi_facade.get_tokenized_sizes()
            )
            // self._world_size
        )

    def __iter__(self) -> Iterator[Tuple[torch.Tensor, torch.Tensor]]:
        worker_info = get_worker_info()
        worker_id = 0 if worker_info is None else worker_info.id
        num_workers = 1 if worker_info is None else worker_info.num_workers

        if self._need_shuffle:
            lines_iterator = iter(self._get_file_lines(worker_id, num_workers))
            lines_bucket = True
            while lines_bucket:
                lines_bucket = list(itertools.islice(lines_iterator, self._shuffle_bucket))
                examples_bucket = [example for line in lines_bucket for example in self._prepare_line(line)]
                shuffle(examples_bucket)
                yield from examples_bucket
        else:
            yield from (
                example for line in self._get_file_lines(worker_id, num_workers) for example in self._prepare_line(line)
            )

    def _get_file_lines(self, worker_id: int, num_workers: int) -> Iterator[str]:
        assert worker_id < num_workers
        # Assuming that num_workers are the same in the whole world
        world_size = self._world_size * num_workers
        rank = self._rank * num_workers + worker_id

        with open(self._data_path, "r") as f:
            for i, line in enumerate(f):
                if i % world_size == rank:
                    yield line

    def _prepare_line(self, line: str) -> List[Tuple[torch.Tensor, torch.Tensor]]:
        res = self._psi_facade.transform(line, to_filter=True)
        if res is None:
            return []

        _, ids = res
        examples = []
        for start in range(0, len(ids), int((1 - self._overlap_slicing) * self._example_len)):
            inp = torch.tensor(ids[start : start + self._example_len], dtype=torch.long)
            labels = torch.tensor(ids[start + 1 : start + self._example_len + 1], dtype=torch.long)
            if self._pad_overlapped and start:
                labels[: int(self._overlap_slicing * self._example_len)] = self._labels_pad
            examples.append((inp, labels))
        return examples