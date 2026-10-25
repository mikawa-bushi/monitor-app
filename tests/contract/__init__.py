"""安全網テスト(公開契約・アップグレード・E2E・既知バグ)の共有モジュール。

テストからは ``from contract import snapshot`` のように import する(pytest の既定の
import モードでは tests/ が import パスに入る)。
"""
