"""Discord bot の feature package 群。

`bot.py` は composition root として残し、feature package が UI/domain logic を
所有する。各 subpackage は 1 つの feature area を持つ。小さな feature は
`models.py` + `presentation.py` から始め、振る舞いが複雑化して依存境界を
明確にする必要が出た時だけ `domain.py` や `application.py` を追加する。
"""
