# -*- coding: utf-8 -*-
"""資金費率量化評分模組（獨立、無外部依賴，只用標準庫）。

== 設計目的 ==
永續合約資金費率反映多空雙方的擁擠程度：正費率＝多方付錢給空方（多頭擁擠），
負費率反之。費率的「極端值」常被視為反轉警示（多頭過度擁擠易 long squeeze 回檔，
空頭過度擁擠易 short squeeze 軋空）——這是「逆向」訊號，跟 ta_scoring.py 那些
動能類分數（正值＝看多）方向定義相反，混合使用時要留意正負號對齊，不要直接
同號疊加。

本檔不抓資料、不管交易所是誰——只吃資金費率歷史序列（純數字 list），輸出分數，
跟 ta_scoring.py 同一套設計原則：同一套公式可以同時餵給不同交易所/不同專案模組。

== 誠實限制 ==
費率反映的是「當下情緒擁擠度」，不是價格預測；業界文獻對其領先/落後於價格的
關係看法不一（有研究認為費率變化常是價格動能的落後結果而非領先指標），
具體績效數字（如某些行銷文章宣稱的年化報酬）可信度存疑，本檔只採用「費率極端
代表擁擠/反轉風險」這個邏輯本身，不承諾任何報酬率。縮放係數 k 是起點參數，
尚未用歷史資料回測校準。
"""


def clamp(v, lo=-100, hi=100):
    return max(lo, min(hi, v))


def score_funding_zscore(funding_now, funding_history, k=25):
    """用該幣種自己過去的資金費率分布做 z-score，而非固定百分比門檻——
    不同幣種、不同市場階段的「正常」費率水準差很多（活躍山寨幣常態費率本來就比
    BTC 高），固定門檻（如「費率 > 0.1% 就是極端」）對高費率幣種永遠觸發、對
    低費率幣種永遠不觸發，是本專案在 DOM/鯨魚門檻/即時警報都踩過的同一種坑。

    funding_history 不含 funding_now 本身，至少要有 2 筆才能算標準差，
    資料不足或標準差趨近 0（費率長期不變）回傳 None／0，不強行給出無意義的極端值。

    回傳負號：費率極端偏多（多頭擁擠）→ 視為反轉風險偏空，給負分；
    費率極端偏空（空頭擁擠）→ 視為反轉風險偏多，給正分。這是「逆向」訊號，
    跟 ta_scoring.py 的動能類分數方向定義相反，合成到總分時不要直接同號疊加。
    """
    if funding_now is None or not funding_history or len(funding_history) < 2:
        return None
    mean = sum(funding_history) / len(funding_history)
    var = sum((x - mean) ** 2 for x in funding_history) / len(funding_history)
    std = var ** 0.5
    if std < 1e-12:
        return 0
    z = (funding_now - mean) / std
    return clamp(round(-z * k))
