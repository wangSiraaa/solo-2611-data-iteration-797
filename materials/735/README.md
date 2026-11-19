# 合成功率与需量合同
power为非负有理数，时间为合成分钟；积分单位是功率×分钟。本题合同的需量为完整15分钟滑窗平均，允许窗口起点不是原段起点。超额费用=max(0,最大需量-6)×3，用精确分数，不另舍入。

p1最大需量6，最早窗[5,20)，累计90、平均6，费用0。p2末段功率9后最优[25,40)，积分100、需量20/3，费用2。只在原段起点0/10/20取最早窗会给错起点；把采样最大8或9当需量也错。

运行`python3 bill_reader.py reader-example.json`确认只读账单格式。example仅格式示例。正式demand-bill/v1保留settlement_id、coverage(complete/partial/insufficient)、window、demand、charge。window两端与数值均为整数n、正整数d的分数对象；不足时window/demand为null，非complete时charge为null，可增来源证据字段。
