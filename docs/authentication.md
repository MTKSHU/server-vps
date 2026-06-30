# 认证与 SSO

server-vps 当前支持两类登录方式：

- 本地账号密码：默认启用，无外部依赖。
- 外部 SSO：可选，支持 CAS 和 OIDC Provider。

Casdoor 已独立于本仓库运行。server-vps 不负责启动、迁移、反代或配置 Casdoor SMTP。

## 默认本地账号

默认平台只显示本地账号登录。初始化时会创建或修正管理员账号：

```text
用户名：admin
密码：ADMIN_INITIAL_PASSWORD
```

用户由管理员在“用户管理”里创建、启用、分组和分配额度。

## 平台自助注册

平台自助注册默认关闭。管理员可登录后进入“平台设置”调整：

- 是否启用平台账号登录。
- 是否允许平台注册。
- 注册用户是否自动启用。
- 平台注册默认分组。

注册表单只收集用户名、密码和电子邮箱，不发送邮箱验证码。

推荐生产默认：

- 注册用户先写入平台用户表并使用默认分组配额。
- 管理员在“用户管理”中审核启用后，用户才能登录。

如果需要即注册即用，可在“平台设置”中打开“平台注册自动启用”。

## 外部 OIDC

管理员登录后进入“平台设置”，在“SSO Provider 配置”中启用 SSO，Provider 类型选择 `OIDC`。

以独立 Casdoor 为例，填写：

- Provider 标识：`casdoor`
- 登录按钮名称：`统一认证`
- 回调基础地址：`https://hpc.example.com`
- OIDC Issuer：`https://auth.example.com`
- Client ID / Client Secret：来自外部 IdP 应用配置
- Scopes：`openid profile email`

如果 Provider 不提供完整 OIDC Discovery，在平台设置中显式填写端点：

```text
授权端点：https://auth.example.com/login/oauth/authorize
令牌端点：https://auth.example.com/api/login/oauth/access_token
用户信息端点：https://auth.example.com/api/userinfo
```

回调地址固定为：

```text
https://hpc.example.com/login/callback
```

需要在外部 IdP 的应用配置里加入这个 redirect URI。

## 外部 CAS

管理员登录后进入“平台设置”，Provider 类型选择 `CAS`，填写 Provider 标识、登录按钮名称、回调基础地址、CAS 服务地址和 CAS 版本。

## 用户创建策略

SSO Provider 接入参数和用户创建策略都由管理员在“平台设置”中调整。

推荐生产默认：

- 自动创建用户记录。
- 新用户默认禁用。
- 管理员在用户管理页审核后启用。

这样可以保留统一认证的登录入口，同时由平台控制资源额度。

## Casdoor 待审用户导入

如果希望用户管理页展示“已在 Casdoor 注册但尚未登录平台”的用户，在“平台设置”的“Casdoor 待审导入”中填写 Casdoor 地址、Client ID、Client Secret 和 Owner。

这些配置用于同步 Casdoor 已注册用户到平台待审核列表；为空时不会请求 Casdoor，不影响已登录 SSO 用户的自动创建与审核流程。

## 独立部署注意事项

- Casdoor 的 SMTP、验证码、数据库迁移和密码迁移脚本应放在 Casdoor 项目中维护。
- server-vps 的 nginx 不再提供 `/sso/` 反向代理。
- 如果 backend 容器访问外部认证域名有 hairpin NAT 问题，优先在 DNS 或宿主机网络层解决；确实需要时可给 Compose 增加本地 override，不要把固定域名写回主 compose。
- 修改 SSO 配置后立即生效，不需要重启 backend。
