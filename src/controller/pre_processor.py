from statistics import median
from typing import Literal, Optional, Sequence


class HysteresisPreprocessor:
    def __init__(self, hysteresis:float=10, type:Literal['linear', 'exponential']='linear'):
        self.hysteresis = hysteresis
        self.weight_type: Literal['linear', 'exponential'] = type
        self._current_group = []

    def process(self, values:Sequence[float | int]) -> Optional[float]:
        if not values:
            return None

        if len(values) == 1:
            self._current_group = [values[0]]
            return values[0]

        # Median als robuster Referenzpunkt (unempfindlich gegen Ausreißer)
        med = median(values)

        # Sammle alle Werte innerhalb der Hysterese um den Median (Inlier)
        inliers = []
        inlier_positions = []
        for i, v in enumerate(values):
            if abs(v - med) <= self.hysteresis:
                inliers.append(v)
                inlier_positions.append(i)

        if len(inliers) >= 2:
            self._current_group = inliers
            # Gewichteter Mittelwert der Inlier (Position = Gewicht)
            weights = self._compute_weights(inlier_positions)
            weighted_sum = sum(v * w for v, w in zip(inliers, weights, strict=False))
            return weighted_sum / sum(weights)
        else:
            # Zu wenige Inlier → Median als robuster Fallback
            self._current_group = list(values)
            return med

    def _compute_weights(self, positions: list[int]) -> list[float]:
        """Berechne Gewichte basierend auf Position (neuere Werte = höheres Gewicht)."""
        n = len(positions)
        if self.weight_type == 'linear':
            return [p + 1 for p in range(n)]
        elif self.weight_type == 'exponential':
            return [2 ** i for i in range(n)]
        return [1] * n

    def get_current_group(self):
        return self._current_group.copy()
