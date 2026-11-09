import multiprocessing


def main():
    multiprocessing.freeze_support()
    from .academic_app import main as launch
    launch()


if __name__ == "__main__":
    main()
