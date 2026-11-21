"""Plot the reproducible sensitivity CSVs. Optional dependency: Matplotlib."""
import argparse
from csv import DictReader
from pathlib import Path


def plot(folder):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def read(name):
        with (folder / name).open(encoding="utf-8", newline="") as file:
            return list(DictReader(file))

    points = [row for row in read("sensitivity.csv") if row["scenario"] != "sweep-double-pass"]
    frontier = sorted({(int(row["reference_all_tasks_missed"]), float(row["reference_cost_rub"]) / 1000)
                       for row in read("frontier.csv")})
    fig, ax = plt.subplots(figsize=(10, 5.8), layout="constrained")
    for method, marker, color in (("B0", "o", "#738492"), ("O1", "D", "#26557d")):
        subset = [r for r in points if r["method"] == method]
        ax.scatter([int(r["reference_all_tasks_missed"]) for r in subset],
                   [float(r["reference_cost_rub"]) / 1000 for r in subset],
                   s=34, marker=marker, color=color, alpha=0.75, label=method)
    if frontier:
        ax.scatter(*zip(*frontier), marker="o", facecolors="none", edgecolors="#c75b2a",
                   linewidths=2, s=170, label="Граница среди рассчитанных планов")
    ax.set_title("Переменная стоимость и выполнение заданий", loc="left", fontsize=17)
    ax.set_xlabel("Задания, не выполненные к сроку, из 1 200")
    ax.set_ylabel("Переменная стоимость, тыс. сценарных ₽")
    ax.grid(axis="y", alpha=0.22)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, loc="upper right")
    fig.savefig(folder / "frontier.png", dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study_folder", type=Path)
    plot(parser.parse_args().study_folder)
