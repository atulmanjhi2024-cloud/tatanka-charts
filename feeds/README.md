# Your feed plugins

Put a feed plugin here (a `.py` file): within a few seconds it shows in Connections under the **+** of Futures
feed, Quotes or Data, no restart. Saving it again loads the new code and restarts the connections using it.
`my_feed.py.example` is a starting point: copy it to `my_feed.py` and fill it in. The guide is [docs/FEEDS.md](../docs/FEEDS.md).

Files here are yours: git ignores them, so `git pull` never changes them. They run with the server's rights:
only put code here that you wrote or trust.
