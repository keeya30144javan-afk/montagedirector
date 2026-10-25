import sys


def run() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "make":
        from .cli import main

        return main(sys.argv[2:])
    from .app.main_window import main

    return main()


if __name__ == "__main__":
    sys.exit(run())
