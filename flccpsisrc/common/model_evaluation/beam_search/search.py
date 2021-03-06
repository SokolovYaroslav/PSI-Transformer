import math
from dataclasses import dataclass
from typing import List, Optional

import torch

from flccpsisrc.psi.psi_datapoint.tree_structures.line_breaker import LineBreaker
from flccpsisrc.psi.psi_datapoint.tree_structures.tree_builder import TreeBuilder, ChangeStatus


@dataclass
class Hypothesis:
    ids: List[int]
    tree_builder: TreeBuilder
    score: float
    is_terminated: bool

    def get_normalized_score(self, len_norm_base: float = 5.0, len_norm_pow: float = 0.7) -> float:
        hyp_length = len(self.ids)
        norm_factor = ((len_norm_base + hyp_length) / (len_norm_base + 1)) ** len_norm_pow
        return math.exp(self.score / norm_factor)


class BeamSearch:
    """Beam search algorithm with normalized by length scores"""

    def __init__(
        self,
        vocab_size: int,
        beam_size: int,
        tree_builder: TreeBuilder,
    ):
        self._vocab_size = vocab_size
        self._beam_size = beam_size

        self._length = 1
        self._terminated_hypotheses = []

        self._scores = None
        self._hypotheses = None
        self._sort_mask = None
        self._row_mask = None
        self._tree_builders = [tree_builder]

        self._is_initialized = False
        self._device = None

    def step(self, log_probs: torch.Tensor) -> Optional[torch.Tensor]:
        """Take a single search step.

        Args:
            log_probs: (batch_size, vocab_size)
                the model's log-probabilities over the vocabulary at the current step

        Return:
            sort_mask: (batch_size,)
                indices of the chosen hypotheses in range [0, batch_size)
                it should be used for sorting your model's hidden state
        """
        if not self._is_initialized:
            self._init_state(log_probs)
            self._is_initialized = True
        self._step_check(log_probs)
        log_probs = self._preprocess_log_probs(log_probs)
        sort_mask = self._step(log_probs)

        return sort_mask

    @property
    def terminated_hypotheses(self) -> List[Hypothesis]:
        """List of lists of tuples of terminated hypotheses and theirs scores"""
        return self._terminated_hypotheses

    @property
    def current_hypotheses(self) -> List[Hypothesis]:
        """List of lists of tuples of terminated hypotheses and theirs scores"""
        return [
            Hypothesis(hyp.tolist(), tree_builder, score.item(), is_terminated=False)
            for hyp, tree_builder, score in zip(self._hypotheses, self._tree_builders, self._scores)
        ]

    @property
    def last_predictions(self) -> torch.Tensor:
        """Tensor of last tokens of the current hypotheses with shape (batch_size,).
        Supposed usage: making a batch for a model"""
        assert (
            self._hypotheses is not None and self._hypotheses.size(1) > 0
        ), f"Can't get last predictions if no steps have been performed"
        return self._hypotheses[:, -1]

    @property
    def batch_size(self) -> int:
        """Current batch size"""
        if self._scores is None:
            return 1
        return self._scores.size(0)

    def _init_state(self, log_probs: torch.Tensor):
        assert self._scores is None and self._hypotheses is None
        self._device = log_probs.device
        self._scores = torch.zeros(1, dtype=log_probs.dtype, device=log_probs.device)
        self._hypotheses = torch.empty(1, 0, dtype=torch.long, device=log_probs.device)
        self._row_mask = torch.empty(log_probs.size(1), dtype=torch.bool, device=log_probs.device)

    def _step_check(self, log_probs: torch.Tensor) -> None:
        assert log_probs.size() == (
            self.batch_size,
            self._vocab_size,
        ), f"log_probs must have shape {(self.batch_size, self._vocab_size)}, but {log_probs.size()} was given"

    def _preprocess_log_probs(self, log_probs: torch.Tensor) -> torch.Tensor:
        for row_id, tree_builder in enumerate(self._tree_builders):
            possible_ids = list(tree_builder.get_next_possible_ids())
            self._row_mask[:] = 1
            self._row_mask[possible_ids] = 0
            log_probs[row_id, self._row_mask] = float("-inf")
        return torch.nn.functional.log_softmax(log_probs, dim=-1)

    def _step(self, log_probs: torch.Tensor) -> Optional[torch.Tensor]:
        log_probs.add_(self._scores.unsqueeze(1))
        log_probs = torch.flatten(log_probs)

        samples = []
        tree_builders = []
        sort_mask = []
        sample_scores = []
        sorted_scores, sorted_inds = torch.topk(
            log_probs, k=(1 + LineBreaker.get_num_newline_nodes()) * self._beam_size, sorted=False
        )
        for ind, score in zip(sorted_inds, sorted_scores):
            if torch.isnan(score):
                break
            ind = ind.item()
            hyp_ind, token_ind = divmod(ind, self._vocab_size)
            tree_builder = self._tree_builders[hyp_ind].copy()
            change_status = tree_builder.add_id(token_ind)
            if change_status == ChangeStatus.END_LINE:
                self._save_terminated(hyp_ind, token_ind, score.item(), tree_builder)
            else:
                samples.append(token_ind)
                tree_builders.append(tree_builder)
                sort_mask.append(hyp_ind)
                sample_scores.append(score)
            if len(samples) == self._beam_size:
                break
        if not samples:
            return None
        if len(samples) < self._beam_size:
            print(
                f"There was not enough hypotheses to process!\n"
                f"Samples drafted: {len(samples)}, beam_size: {self._beam_size}"
            )

        self._update_state(samples, sort_mask, sample_scores, tree_builders)
        self._length += 1

        return self._sort_mask

    def _update_state(
        self,
        samples: List[int],
        sort_mask: List[int],
        new_scores: List[float],
        tree_builders: List[TreeBuilder],
    ) -> None:
        self._samples = torch.tensor(samples, dtype=torch.long, device=self._device)
        self._sort_mask = torch.tensor(sort_mask, dtype=torch.long, device=self._device)
        self._scores = torch.tensor(new_scores, dtype=self._scores.dtype, device=self._device)
        self._tree_builders = tree_builders

        self._hypotheses = self._hypotheses[sort_mask]
        self._hypotheses = torch.cat((self._hypotheses, self._samples.unsqueeze(1)), dim=1)

    def _save_terminated(self, hyp_ind: int, sample_ind: int, score: float, tree_builder: TreeBuilder) -> None:
        hyp_inds = self._hypotheses[hyp_ind].tolist()
        hyp_inds.append(sample_ind)
        self._terminated_hypotheses.append(Hypothesis(hyp_inds, tree_builder, score, is_terminated=True))
