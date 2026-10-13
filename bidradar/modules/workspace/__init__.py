"""workspace 模块：公司、成员、三种角色、企业档案版本。

表放在 PostgreSQL 的 workspace schema 里：workspaces、memberships、membership_events、profile_proposals、profile_versions。
对外提供：authorize()、get_profile()。别的模块只能调用本模块 service.py 里的函数。
阶段 1 实现。
"""
