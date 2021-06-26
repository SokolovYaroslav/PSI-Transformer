import argparse
import json
from typing import List

import editdistance
import numpy as np

from src.common.model_evaluation.produce_predictions import Prediction, TextHypothesis


class PredictionPostprocessor:
    def __init__(self, len_norm_base: float, len_norm_pow: float):
        self._len_norm_base = len_norm_base
        self._len_norm_pow = len_norm_pow
        pass

    def get_top_k(self, prediction: Prediction, k: int) -> List[TextHypothesis]:
        sorted_hyps = sorted(
            prediction.hypotheses, key=lambda h: self._normalize_score(h.length, h.score), reverse=True
        )
        return sorted_hyps[:k]

    def _normalize_score(self, length: int, score: float) -> float:
        norm_factor = ((self._len_norm_base + length) / (self._len_norm_base + 1)) ** self._len_norm_pow
        return score / norm_factor


def edit_similarity(a: str, b: str) -> float:
    a, b = a.replace(" ", ""), b.replace(" ", "")
    dist = editdistance.eval(a, b)
    return (1 - (dist / max(len(a), len(b)))) * 100


def evaluate(prediction_paths: str, ks: List[int], len_norm_base: float, len_norm_pow: float) -> None:
    for prediction_path in prediction_paths:
        predictions = []
        with open(prediction_path) as f:
            for line in f:
                pred_dict = json.loads(line)
                predictions.append(Prediction.from_dict(pred_dict))

        pred_postprocessor = PredictionPostprocessor(len_norm_base, len_norm_pow)
        for k in ks:
            scores = []
            for prediction in predictions:
                hyps = pred_postprocessor.get_top_k(prediction, k)
                score = max(edit_similarity(h.prediction, prediction.target) for h in hyps)
                scores.append(score)

            scores = np.array(scores)
            print(
                f"Edit similarity @{k} for {prediction_path}\n"
                f"len_norm_base: {len_norm_base} len_norm_pow: {len_norm_pow}\n"
                f"{scores.mean()} +-{scores.std()}"
            )


if __name__ == "__main__":

    def main():
        args = argparse.ArgumentParser()
        args.add_argument("-p", "--pred_paths", nargs="+", type=str, required=True)
        args.add_argument("-k", "--top_ks", nargs="+", type=int, required=True)
        args.add_argument("--len_norm_base", type=float, default=10.0)
        args.add_argument("--len_norm_pow", type=float, default=0.7)
        args = args.parse_args()

        evaluate(args.pred_paths, args.top_ks, args.len_norm_base, args.len_norm_pow)

    main()
