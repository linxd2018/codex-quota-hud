"""Keep the background launch monitor free of Qt and graphics runtime imports."""
import sys


if __name__ == '__main__':
    if sys.argv[1:] in (['--enable-follow-gpt'], ['--disable-follow-gpt']):
        from follow_gpt import set_enabled
        set_enabled(sys.argv[1] == '--enable-follow-gpt')
        raise SystemExit(0)
    if sys.argv[1:] == ['--follow-gpt']:
        from follow_gpt import monitor
        raise SystemExit(monitor())
    from quota_glass import main
    raise SystemExit(main())
