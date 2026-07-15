"""Ensure data/cache/models.joblib exists (used by Docker build)."""

from __future__ import annotations

import bangkok_bus as bb


def main() -> None:
    bb.load_real_network()
    if bb.load_models() is not None:
        print("using cached models", bb.MODELS_CACHE)
        return
    print("training models…")
    models = bb.train_models(bb.generate_historical_data(60))
    path = bb.save_models(models)
    print("saved", path)


if __name__ == "__main__":
    main()
