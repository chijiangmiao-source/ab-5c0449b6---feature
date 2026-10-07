"""预置示例演练：覆盖环路拒绝、本地偏好裁决、断开切换、滞留消息、
重连纪元递增、过期消息忽略、旧纪元通告/撤销忽略、滞留后路由恢复
投递与恢复收敛校验。"""

ROUTERS_TEXT = "A B C D"

LINKS_TEXT = """\
# 有向邻接：<源> <宿> pref=<本地偏好> [epoch=<起始纪元>]
A B pref=100
B A pref=100
B C pref=100
C B pref=100
C D pref=100
D C pref=100
D A pref=50
A D pref=50
"""

EVENTS_TEXT = """\
# 1) 各路由器始发自身前缀（通告自动泛洪，环路处自动拒绝传播）
announce A A prefix=A
announce B B prefix=B
announce C C prefix=C
announce D D prefix=D
# 2) 手工构造环路：路径含接收方 D 的标识，必须拒绝
announce C D prefix=P9 path=A,D,C epoch=1
# 3) D→A 投递：本地偏好 100 的 C-B-A 路径优于偏好 50 的 D-A 直连
deliver D A msg=hello-via-C
# 4) 断开 D->C：C 失去 D 的直连前缀，A 改走 D->A 直连完成投递
disconnect D C
deliver A D msg=failover-direct
# 5) 断开 D->A：D 完全不可达，C→D 消息滞留待投递
disconnect D A
deliver C D msg=will-expire
# 6) 重连 D->C（纪元 1→2）并按新纪元重发路由；滞留消息过期忽略
reconnect D C
# 7) 旧纪元的通告与撤销均被忽略（存储路由为纪元 2）
announce D C prefix=D path=D epoch=1
withdraw D C prefix=D epoch=1
# 8) 正常投递
deliver C A msg=after-reconnect
# 9) D 撤销本机始发前缀 D：撤销沿全网传播，B 无到 D 的路由，消息滞留
withdraw D D prefix=D
deliver B D msg=stranded-then-restored
# 10) D 重新始发前缀 D：链路纪元均未变化，滞留消息的新尝试按当时
#     最优路径 B-C-D 逐跳投递成功；旧尝试（滞留）原样保留可复核
announce D D prefix=D
"""


def sample_drill():
    return {
        "routers_text": ROUTERS_TEXT,
        "links_text": LINKS_TEXT,
        "events_text": EVENTS_TEXT,
    }
