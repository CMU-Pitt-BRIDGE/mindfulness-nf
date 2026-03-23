import sys


def main() -> None:
    from mindfulness_nf.tui.app import MindfulnessApp

    test_mode = "--test" in sys.argv
    app = MindfulnessApp(test_mode=test_mode)
    app.run()


if __name__ == "__main__":
    main()
