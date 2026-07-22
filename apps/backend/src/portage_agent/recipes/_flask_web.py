"""Deterministic web-surface normalizers for Flask to FastAPI."""

from __future__ import annotations

import ast
import re

from portage_agent.agent.nodes.common import _module_names, _resolve_module

from ._flask_analysis import (
    _FLASK_LOGIN_NAMES,
    _parsed,
)


def _normalize_translation_literals(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "translation_literals" and item.get("path") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    bindings = set(decision.get("bindings", []))

    class Replace(ast.NodeTransformer):
        changed = False

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if (
                ast.unparse(node.func) in bindings and len(node.args) == 1
                and all(keyword.arg is not None for keyword in node.keywords)
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                self.changed = True
                if not node.keywords:
                    return node.args[0]
                return ast.BinOp(
                    left=node.args[0], op=ast.Mod(),
                    right=ast.Dict(
                        keys=[ast.Constant(keyword.arg) for keyword in node.keywords],
                        values=[keyword.value for keyword in node.keywords],
                    ),
                )
            return node

    replace = Replace()
    tree = replace.visit(tree)
    if not replace.changed:
        return content
    remaining = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    for statement in list(tree.body):
        if not (
            isinstance(statement, ast.ImportFrom)
            and statement.module == "flask_babel"
        ):
            continue
        statement.names = [
            alias for alias in statement.names
            if (alias.asname or alias.name) in remaining
        ]
        if not statement.names:
            tree.body.remove(statement)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_fastapi_mail_import(content: str) -> str:
    """Replace the unavailable model-invented package with a small stdlib surface."""
    tree = _parsed(content)
    if tree is None:
        return content
    supported = {"ConnectionConfig", "FastMail", "MessageSchema", "MessageType"}
    bindings: dict[str, str] = {}
    insertion = None
    for statement in list(tree.body):
        if not isinstance(statement, ast.ImportFrom) or statement.module != "fastapi_mail":
            continue
        kept = []
        for alias in statement.names:
            if alias.name in supported:
                bindings[alias.name] = alias.asname or alias.name
            else:
                kept.append(alias)
        if len(kept) == len(statement.names):
            continue
        insertion = tree.body.index(statement) if insertion is None else insertion
        statement.names = kept
        if not kept:
            tree.body.remove(statement)
    if not bindings or insertion is None:
        return content

    definitions = {
        node.name for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    additions: list[ast.stmt] = []
    for original in ("ConnectionConfig", "MessageSchema"):
        local = bindings.get(original)
        if local and local not in definitions:
            additions.extend(ast.parse(
                f"class {local}:\n"
                "    def __init__(self, **values):\n"
                "        self.__dict__.update(values)\n"
            ).body)
    message_type = bindings.get("MessageType")
    if message_type and message_type not in definitions:
        additions.extend(ast.parse(
            f"class {message_type}:\n"
            "    plain = 'plain'\n"
            "    html = 'html'\n"
        ).body)
    fast_mail = bindings.get("FastMail")
    if fast_mail and fast_mail not in definitions:
        additions.extend([
            ast.Import(names=[ast.alias(name="smtplib", asname="_portage_smtplib")]),
            ast.ImportFrom(
                module="email.message",
                names=[ast.alias(name="EmailMessage", asname="_PortageEmailMessage")],
                level=0,
            ),
        ])
        additions.extend(ast.parse(
            f"class {fast_mail}:\n"
            "    def __init__(self, config):\n"
            "        self.config = config\n"
            "    async def send_message(self, message):\n"
            "        email = _PortageEmailMessage()\n"
            "        email['Subject'] = str(getattr(message, 'subject', ''))\n"
            "        sender = getattr(message, 'sender', None) or "
            "getattr(self.config, 'MAIL_FROM', '')\n"
            "        recipients = list(getattr(message, 'recipients', ()) or ())\n"
            "        email['From'] = str(sender)\n"
            "        email['To'] = ', '.join(map(str, recipients))\n"
            "        body = str(getattr(message, 'body', ''))\n"
            "        subtype = str(getattr(message, 'subtype', 'plain')).lower()\n"
            "        if subtype.endswith('html'):\n"
            "            email.add_alternative(body, subtype='html')\n"
            "        else:\n"
            "            email.set_content(body)\n"
            "        for attachment in getattr(message, 'attachments', ()) or ():\n"
            "            if isinstance(attachment, tuple) and len(attachment) == 3:\n"
            "                filename, content_type, data = attachment\n"
            "                main, _, sub = str(content_type).partition('/')\n"
            "                email.add_attachment(\n"
            "                    data, maintype=main or 'application',\n"
            "                    subtype=sub or 'octet-stream', filename=filename\n"
            "                )\n"
            "        use_ssl = bool(getattr(self.config, 'MAIL_SSL_TLS', False))\n"
            "        client_type = (_portage_smtplib.SMTP_SSL if use_ssl "
            "else _portage_smtplib.SMTP)\n"
            "        server = getattr(self.config, 'MAIL_SERVER', 'localhost')\n"
            "        port = int(getattr(self.config, 'MAIL_PORT', 465 if use_ssl else 25))\n"
            "        with client_type(server, port) as client:\n"
            "            if not use_ssl and getattr(self.config, 'MAIL_STARTTLS', False):\n"
            "                client.starttls()\n"
            "            username = getattr(self.config, 'MAIL_USERNAME', None)\n"
            "            password = getattr(self.config, 'MAIL_PASSWORD', None)\n"
            "            if username:\n"
            "                client.login(username, password or '')\n"
            "            client.send_message(email)\n"
        ).body)
    tree.body[insertion:insertion] = additions
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_template_response(content: str) -> str:
    """Mechanically upgrade the one deprecated Starlette template call shape.

    GPT-4o repeatedly reproduces the old API after exact feedback. This transform is
    framework-level and semantics-preserving: it only runs when a real Jinja2Templates
    instance and an enclosing request argument make the rewrite unambiguous.
    """
    tree = _parsed(content)
    if tree is None:
        return content
    instances = {
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and (
            isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "Jinja2Templates"
            or isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "Jinja2Templates"
        )
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    }
    if not instances:
        return content
    direct_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"starlette.responses", "fastapi.responses"}
        for alias in statement.names if alias.name == "TemplateResponse"
    }

    class Upgrade(ast.NodeTransformer):
        def __init__(self):
            self.requests: list[str] = []
            self.changed = False

        def _function(self, node):
            positional = [*node.args.posonlyargs, *node.args.args]
            self.requests.append(positional[0].arg if positional else "")
            node = self.generic_visit(node)
            self.requests.pop()
            return node

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            request_name = self.requests[-1] if self.requests else ""
            if not request_name or not node.args:
                return node
            direct = isinstance(node.func, ast.Name) and node.func.id in direct_names
            bound = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in instances
            )
            if not (direct or bound):
                return node
            if (
                isinstance(node.args[0], ast.Name)
                and node.args[0].id == request_name
            ):
                return node
            context = node.args[1] if len(node.args) > 1 else ast.Dict(keys=[], values=[])
            if isinstance(context, ast.Dict):
                kept = [
                    (key, value) for key, value in zip(
                        context.keys, context.values, strict=True,
                    )
                    if not (
                        isinstance(key, ast.Constant) and key.value == "request"
                    )
                ]
                context = ast.Dict(
                    keys=[key for key, _ in kept],
                    values=[value for _, value in kept],
                )
            node.func = ast.Attribute(
                value=ast.Name(id=sorted(instances)[0], ctx=ast.Load()),
                attr="TemplateResponse", ctx=ast.Load(),
            )
            node.args = [
                ast.Name(id=request_name, ctx=ast.Load()), node.args[0], context,
            ]
            node.keywords = [kw for kw in node.keywords if kw.arg != "request"]
            self.changed = True
            return node

    upgrade = Upgrade()
    tree = upgrade.visit(tree)
    if not upgrade.changed:
        return content
    for statement in list(tree.body):
        if not (
            isinstance(statement, ast.ImportFrom)
            and statement.module in {"starlette.responses", "fastapi.responses"}
        ):
            continue
        statement.names = [
            alias for alias in statement.names if alias.name != "TemplateResponse"
        ]
        if not statement.names:
            tree.body.remove(statement)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_exception_handler_status(content: str) -> str:
    """Turn Flask ``(response, status)`` tuples into a real FastAPI Response status."""
    tree = _parsed(content)
    if tree is None:
        return content

    class Normalize(ast.NodeTransformer):
        def __init__(self):
            self.handler_statuses: list[int | None] = []
            self.changed = False

        def _function(self, node):
            status = next((
                decorator.args[0].value
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and ast.unparse(decorator.func).split(".")[-1] == "exception_handler"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, int)
            ), None)
            self.handler_statuses.append(status)
            node = self.generic_visit(node)
            self.handler_statuses.pop()
            return node

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

        def visit_Return(self, node):  # noqa: N802
            node = self.generic_visit(node)
            status = self.handler_statuses[-1] if self.handler_statuses else None
            tuple_response = (
                isinstance(node.value, ast.Tuple)
                and len(node.value.elts) == 2
                and isinstance(node.value.elts[1], ast.Constant)
                and isinstance(node.value.elts[1].value, int)
                and isinstance(node.value.elts[0], ast.Call)
            )
            rendered_status = (
                status is not None
                and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func).split(".")[-1] == "render_template"
                and any(
                    keyword.arg == "status_code"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == status
                    for keyword in node.value.keywords
                )
            )
            if not tuple_response and not rendered_status:
                return node
            response, status_node = (
                node.value.elts if tuple_response else (node.value, ast.Constant(status))
            )
            self.changed = True
            return [
                ast.Assign(
                    targets=[ast.Name(id="_portage_response", ctx=ast.Store())],
                    value=response,
                ),
                ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id="_portage_response", ctx=ast.Load()),
                        attr="status_code", ctx=ast.Store(),
                    )],
                value=status_node,
                ),
                ast.Return(value=ast.Name(id="_portage_response", ctx=ast.Load())),
            ]

    normalizer = Normalize()
    tree = normalizer.visit(tree)
    if not normalizer.changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_template_consumers(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Pass an endpoint's request into the frozen request-first render provider."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "template_runtime" and path in item.get("files", [])
        and path not in item.get("provider_files", [])
    ]
    tree = _parsed(content)
    if not decisions or tree is None:
        return content
    provider_functions = {
        provider: set(names)
        for decision in decisions
        for provider, names in decision.get("provider_functions", {}).items()
    }
    required = {
        name
        for decision in decisions
        for name in decision.get("consumer_functions", {}).get(path, [])
    }
    owners = {
        name: [provider for provider, names in provider_functions.items() if name in names]
        for name in required
    }
    owned = {name: providers[0] for name, providers in owners.items() if len(providers) == 1}
    imports_changed = False

    # Models often reproduce a tiny local TemplateResponse wrapper or import a frozen
    # function from the wrong created artifact. The source and accepted provider plan
    # already decide both facts, so wire that decision mechanically.
    template_helpers = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(call, ast.Call)
            and ast.unparse(call.func).split(".")[-1] == "TemplateResponse"
            for call in ast.walk(node)
        )
    }
    aliases: dict[str, str] = {}
    if "render_template" in owned and "render_template" not in {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } and len(template_helpers) == 1:
        aliases[template_helpers.pop()] = "render_template"
    for statement in list(tree.body):
        if not isinstance(statement, ast.ImportFrom):
            continue
        module = _resolve_module(statement.module, statement.level, path)
        kept = []
        for alias in statement.names:
            if alias.name in owned and module not in _module_names(owned[alias.name]):
                aliases[alias.asname or alias.name] = alias.name
                continue
            kept.append(alias)
        if len(kept) != len(statement.names):
            imports_changed = True
            statement.names = kept
            if not kept:
                tree.body.remove(statement)

    if aliases:
        class ReplaceTemplateAliases(ast.NodeTransformer):
            def visit_Name(self, node):  # noqa: N802
                replacement = aliases.get(node.id)
                return ast.Name(id=replacement, ctx=node.ctx) if replacement else node

        tree = ReplaceTemplateAliases().visit(tree)
        tree.body = [
            statement for statement in tree.body
            if not (
                isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name in aliases
            )
        ]
        imports_changed = True

    for name, provider in sorted(owned.items()):
        if not any(
            isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            and node.id == name for node in ast.walk(tree)
        ):
            continue
        imported = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(provider)
        ), None)
        if imported is None:
            imported = ast.ImportFrom(
                module=provider.removesuffix(".py").replace("/", "."),
                names=[], level=0,
            )
            tree.body.insert(0, imported)
        if not any(alias.name == name for alias in imported.names):
            imported.names.append(ast.alias(name=name))
            imports_changed = True
    loaded_names = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    for statement in list(tree.body):
        if not isinstance(statement, ast.ImportFrom):
            continue
        module = _resolve_module(statement.module, statement.level, path)
        provider = next((
            candidate for candidate in provider_functions
            if module in _module_names(candidate)
        ), "")
        if not provider:
            continue
        kept = []
        for alias in statement.names:
            local = alias.asname or alias.name
            if (
                alias.name == "*" or alias.name in provider_functions[provider]
                or local in loaded_names
            ):
                kept.append(alias)
            else:
                imports_changed = True
        statement.names = kept
        if not kept:
            tree.body.remove(statement)
    request_first = {
        (provider, name)
        for decision in decisions
        for provider, names in decision.get("provider_functions", {}).items()
        for name in names if name == "render_template"
    }
    local_names: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        module = _resolve_module(statement.module, statement.level, path)
        for provider, name in request_first:
            if module not in _module_names(provider):
                continue
            local_names.update(
                alias.asname or alias.name
                for alias in statement.names if alias.name == name
            )
    if not local_names:
        if not imports_changed:
            return content
        ast.fix_missing_locations(tree)
        return ast.unparse(tree) + "\n"

    request_types = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"fastapi", "starlette.requests"}
        for alias in statement.names if alias.name == "Request"
    }
    request_type = next(iter(request_types), "Request")
    signature_changed = False
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {
                "api_route", "delete", "get", "head", "options", "patch", "post", "put",
            }
            for decorator in node.decorator_list
        )
        and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id in local_names
            for call in ast.walk(node)
        )
    ):
        arguments = [
            *function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs,
        ]
        if any(
            argument.arg == "request"
            or argument.annotation is not None
            and ast.unparse(argument.annotation).split(".")[-1] == "Request"
            for argument in arguments
        ):
            continue
        insert_at = len(function.args.args) - len(function.args.defaults)
        function.args.args.insert(insert_at, ast.arg(
            arg="request", annotation=ast.Name(id=request_type, ctx=ast.Load()),
        ))
        signature_changed = True
    if signature_changed and not request_types:
        tree.body.insert(0, ast.ImportFrom(
            module="starlette.requests", names=[ast.alias(name="Request")], level=0,
        ))

    class Realize(ast.NodeTransformer):
        def __init__(self):
            self.requests: list[str] = []
            self.changed = False

        def _function(self, node):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            request = next((
                argument.arg for argument in arguments
                if argument.arg == "request"
                or argument.annotation is not None
                and ast.unparse(argument.annotation).split(".")[-1] == "Request"
            ), "")
            self.requests.append(request)
            node = self.generic_visit(node)
            self.requests.pop()
            return node

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            request = self.requests[-1] if self.requests else ""
            if (
                request and isinstance(node.func, ast.Name)
                and node.func.id in local_names
                and not (
                    node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == request
                )
            ):
                node.args.insert(0, ast.Name(id=request, ctx=ast.Load()))
                self.changed = True
            return node

    realize = Realize()
    tree = realize.visit(tree)
    if not (imports_changed or signature_changed or realize.changed):
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_authentication_consumers(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "authentication_runtime"
        and path in item.get("consumer_bindings", {})
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    bindings = decision["consumer_bindings"][path]
    provider = decision["provider"].removesuffix(".py").replace("/", ".")

    replacements = {
        item["source_ref"]: item["local"]
        for item in bindings if "." in item["source_ref"]
    }

    class ReplaceModuleBindings(ast.NodeTransformer):
        def visit_Attribute(self, node):  # noqa: N802
            node = self.generic_visit(node)
            replacement = replacements.get(ast.unparse(node))
            return ast.Name(id=replacement, ctx=node.ctx) if replacement else node

    tree = ReplaceModuleBindings().visit(tree)
    locals_ = {item["local"] for item in bindings}
    for statement in list(tree.body):
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if statement.name in locals_:
                tree.body.remove(statement)
        elif isinstance(statement, ast.ImportFrom) and statement.module == "flask_login":
            statement.names = [
                alias for alias in statement.names
                if alias.name not in _FLASK_LOGIN_NAMES
            ]
            if not statement.names:
                tree.body.remove(statement)
        elif isinstance(statement, ast.Import):
            statement.names = [
                alias for alias in statement.names if alias.name != "flask_login"
            ]
            if not statement.names:
                tree.body.remove(statement)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(isinstance(target, ast.Name) and target.id in locals_ for target in targets):
                tree.body.remove(statement)

    imported = next((
        statement for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and _resolve_module(statement.module, statement.level, path) in _module_names(
            decision["provider"]
        )
    ), None)
    if imported is None:
        imported = ast.ImportFrom(module=provider, names=[], level=0)
        insert_at = 1 if (
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ) else 0
        while insert_at < len(tree.body) and isinstance(
            tree.body[insert_at], (ast.Import, ast.ImportFrom)
        ):
            insert_at += 1
        tree.body.insert(insert_at, imported)
    present = {(alias.name, alias.asname) for alias in imported.names}
    for item in bindings:
        alias = (item["symbol"], None if item["local"] == item["symbol"] else item["local"])
        if alias not in present:
            imported.names.append(ast.alias(name=alias[0], asname=alias[1]))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_template_provider_globals(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "template_runtime"
        and path in item.get("provider_files", [])
        and "current_user" in item.get("context_globals", [])
        and item.get("authentication_provider")
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    provider_path = decision["authentication_provider"]
    provider = provider_path.removesuffix(".py").replace("/", ".")
    imported = next((
        statement for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and _resolve_module(statement.module, statement.level, path) in _module_names(
            provider_path
        )
    ), None)
    if imported is not None:
        imported.names = [
            alias for alias in imported.names if alias.name != "current_user"
        ]
        if not imported.names:
            tree.body.remove(imported)
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "render_template"
    ):
        if not any(
            isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(provider_path)
            and any(alias.name == "current_user" for alias in statement.names)
            for statement in function.body
        ):
            function.body.insert(0, ast.ImportFrom(
                module=provider, names=[ast.alias(name="current_user")], level=0,
            ))
        values = next((
            statement.value for statement in function.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "values"
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
            and isinstance(statement.value, ast.Dict)
        ), None)
        if values is not None and not any(
            isinstance(key, ast.Constant) and key.value == "current_user"
            for key in values.keys
        ):
            values.keys.append(ast.Constant("current_user"))
            values.values.append(ast.Name(id="current_user", ctx=ast.Load()))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_template_context_processors(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Keep source context processors as request-time template context providers."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "template_context_processors"
        and path in item.get("files", [])
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content

    changed = False
    if path in decision.get("factory_files", []):
        for contract in (
            item for item in decision.get("processors", [])
            if item["provider"] == path
        ):
            factory = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == contract["factory"]
            ), None)
            if factory is None:
                continue
            callback = next((
                node for node in factory.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == contract["function"]
            ), None)
            if contract.get("source"):
                parsed = _parsed(contract["source"])
                if parsed is not None and len(parsed.body) == 1:
                    replacement = parsed.body[0]
                    replacement.decorator_list = []
                    if callback is None:
                        insertion = next((
                            index for index, statement in enumerate(factory.body)
                            if isinstance(statement, ast.Return)
                        ), len(factory.body))
                        factory.body.insert(insertion, replacement)
                    else:
                        factory.body[factory.body.index(callback)] = replacement
                    callback = replacement
                    changed = True
            elif callback is not None and callback.decorator_list:
                callback.decorator_list = []
                changed = True
            if callback is None:
                continue

        callbacks = [
            item["function"] for item in decision.get("processors", [])
            if item["provider"] == path
            and any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == item["function"]
                for factory in tree.body
                if isinstance(factory, (ast.FunctionDef, ast.AsyncFunctionDef))
                and factory.name == item["factory"]
                for node in factory.body
            )
        ]
        for factory in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                item["provider"] == path and item["factory"] == node.name
                for item in decision.get("processors", [])
            )
        ):
            receiver = next(
                item["receiver"] for item in decision["processors"]
                if item["provider"] == path and item["factory"] == factory.name
            )
            target = f"{receiver}.state._portage_context_processors"
            factory.body = [
                statement for statement in factory.body
                if not (
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and any(
                        ast.unparse(candidate) == target
                        for candidate in (
                            statement.targets if isinstance(statement, ast.Assign)
                            else [statement.target]
                        )
                    )
                )
            ]
            registered = [
                name for name in callbacks
                if any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name for node in factory.body
                )
            ]
            if registered:
                assignment = ast.Assign(
                    targets=[ast.parse(target, mode="eval").body],
                    value=ast.Tuple(
                        elts=[ast.Name(id=name, ctx=ast.Load()) for name in registered],
                        ctx=ast.Load(),
                    ),
                )
                insertion = next((
                    index for index, statement in enumerate(factory.body)
                    if isinstance(statement, ast.Return)
                ), len(factory.body))
                factory.body.insert(insertion, assignment)
                changed = True

    if path in decision.get("template_provider_files", []):
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "render_template"
        ):
            if any(
                isinstance(node, ast.Constant)
                and node.value == "_portage_context_processors"
                for node in ast.walk(function)
            ):
                continue
            values_at = next((
                index for index, statement in enumerate(function.body)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == "values"
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
            ), None)
            if values_at is None:
                continue
            function.body.insert(values_at + 1, ast.parse(
                "for _portage_context_processor in getattr("
                "request.app.state, '_portage_context_processors', ()):"
                "\n    values.update(_portage_context_processor())"
            ).body[0])
            changed = True

    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_redirect_urls(content: str) -> str:
    """Keep Flask's relative ``url_for`` default in RedirectResponse calls."""
    tree = _parsed(content)
    if tree is None:
        return content

    class RelativeRedirect(ast.NodeTransformer):
        changed = False

        def visit_Call(self, node):  # noqa: N802
            self.generic_visit(node)
            if ast.unparse(node.func).split(".")[-1] != "RedirectResponse":
                return node
            targets = [keyword for keyword in node.keywords if keyword.arg == "url"]
            values = [target.value for target in targets] or node.args[:1]
            for value in values:
                if not (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "url_for"
                ):
                    continue
                relative = ast.Attribute(value=value, attr="path", ctx=ast.Load())
                if targets:
                    targets[0].value = relative
                else:
                    node.args[0] = relative
                self.changed = True
            return node

    normalizer = RelativeRedirect()
    tree = normalizer.visit(tree)
    if not normalizer.changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_werkzeug_abort(content: str) -> str:
    """Replace Flask/Werkzeug's implicit abort handling with FastAPI HTTPException."""
    tree = _parsed(content)
    if tree is None:
        return content
    abort_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module == "werkzeug.exceptions"
        for alias in statement.names if alias.name == "abort"
    }
    uses_http_exception = any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        and node.id == "HTTPException"
        for node in ast.walk(tree)
    )
    imported_http_exception = any(
        isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        and any(alias.name == "HTTPException" for alias in statement.names)
        for statement in tree.body
    )
    if not abort_names and (not uses_http_exception or imported_http_exception):
        return content
    for statement in list(tree.body):
        if not (
            isinstance(statement, ast.ImportFrom)
            and statement.module == "werkzeug.exceptions"
        ):
            continue
        statement.names = [alias for alias in statement.names if alias.name != "abort"]
        if not statement.names:
            tree.body.remove(statement)
    http_exception = next((
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        for alias in statement.names if alias.name == "HTTPException"
    ), None)
    if http_exception is None:
        fastapi_import = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        ), None)
        if fastapi_import is None:
            fastapi_import = ast.ImportFrom(
                module="fastapi", names=[ast.alias(name="HTTPException")], level=0,
            )
            import_at = 1 if (
                tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)
            ) else 0
            while (
                import_at < len(tree.body)
                and isinstance(tree.body[import_at], ast.ImportFrom)
                and tree.body[import_at].module == "__future__"
            ):
                import_at += 1
            tree.body.insert(import_at, fastapi_import)
        else:
            fastapi_import.names.append(ast.alias(name="HTTPException"))
        http_exception = "HTTPException"
    defined = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    helpers = [
        ast.parse(
            f"def {name}(status_code, description=None):\n"
            f"    raise {http_exception}(status_code=status_code, detail=description)"
        ).body[0]
        for name in sorted(abort_names - defined)
    ]
    insert_at = 1 if (
        tree.body and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ) else 0
    while insert_at < len(tree.body) and isinstance(
        tree.body[insert_at], (ast.Import, ast.ImportFrom)
    ):
        insert_at += 1
    tree.body[insert_at:insert_at] = helpers
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_mutated_fetchone_rows(content: str) -> str:
    """Materialize only fetched rows that generated code later mutates."""
    tree = _parsed(content)
    if tree is None:
        return content
    helper_name = "_portage_mutable_row"
    changed = False
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != helper_name
    ):
        mutated = {
            node.value.id for node in ast.walk(function)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
        }
        for assignment in (
            node for node in ast.walk(function)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ):
            targets = (
                assignment.targets if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            if not any(
                isinstance(target, ast.Name) and target.id in mutated
                for target in targets
            ):
                continue
            value = assignment.value
            if not (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "fetchone"
            ):
                continue
            assignment.value = ast.Call(
                func=ast.Name(id=helper_name, ctx=ast.Load()),
                args=[value], keywords=[],
            )
            changed = True
    if not changed:
        return content
    if not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == helper_name for node in tree.body
    ):
        helper = ast.parse(
            f"def {helper_name}(value):\n"
            "    return dict(value) if value is not None else None\n"
        ).body[0]
        insert_at = 1 if (
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ) else 0
        while insert_at < len(tree.body) and isinstance(
            tree.body[insert_at], (ast.Import, ast.ImportFrom)
        ):
            insert_at += 1
        tree.body.insert(insert_at, helper)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_session_middleware_import(content: str) -> str:
    """FastAPI re-exports no sessions module; Starlette owns this middleware."""
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    for statement in tree.body:
        if (
            isinstance(statement, ast.ImportFrom)
            and statement.module == "fastapi.middleware.sessions"
        ):
            statement.module = "starlette.middleware.sessions"
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_request_hook_names(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Restore a frozen hook name when generation only added a dependency suffix."""
    hooks = {
        hook["function"]: hook
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "request_hooks" and decision.get("path") == path
        for hook in decision.get("hooks", []) if hook.get("function")
    }
    tree = _parsed(content)
    if not hooks or tree is None:
        return content
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    changed = False
    for name in sorted(hooks.keys() - functions.keys()):
        candidates = [
            functions.get(f"{name}{suffix}")
            for suffix in ("_dep", "_dependency")
            if functions.get(f"{name}{suffix}") is not None
        ]
        if len(candidates) == 1:
            function = candidates[0]
            old_name = function.name
            function.name = name
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == old_name:
                    node.id = name
        elif source := hooks[name].get("source"):
            parsed = _parsed(source)
            if parsed is None or len(parsed.body) != 1 or not isinstance(
                parsed.body[0], (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            insert_at = 1 if (
                tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)
            ) else 0
            while insert_at < len(tree.body) and isinstance(
                tree.body[insert_at], (ast.Import, ast.ImportFrom)
            ):
                insert_at += 1
            tree.body.insert(insert_at, parsed.body[0])
        else:
            continue
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_error_handler_ownership(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Let app-owned exception handlers preserve their source JSON envelopes."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "error_handler_ownership"
        and path in item.get("route_functions", {})
    ]
    route_functions = {
        name for item in decisions for name in item["route_functions"].get(path, [])
    }
    handled = {
        contract["exception_name"]
        for item in decisions for contract in item.get("handlers", [])
    }
    tree = _parsed(content)
    if tree is None or not route_functions or not handled:
        return content

    class RemoveOwnedHandlers(ast.NodeTransformer):
        changed = False

        def visit_FunctionDef(self, node):  # noqa: N802
            if node.name in route_functions:
                self.generic_visit(node)
            return node

        def visit_AsyncFunctionDef(self, node):  # noqa: N802
            if node.name in route_functions:
                self.generic_visit(node)
            return node

        def visit_Try(self, node):  # noqa: N802
            self.generic_visit(node)
            kept = [
                handler for handler in node.handlers
                if handler.type is None
                or ast.unparse(handler.type).split(".")[-1] not in handled
            ]
            if len(kept) == len(node.handlers):
                return node
            self.changed = True
            node.handlers = kept
            if node.handlers or node.finalbody:
                return node
            return [*node.body, *node.orelse]

    normalizer = RemoveOwnedHandlers()
    tree = normalizer.visit(tree)
    if not normalizer.changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_blueprint_error_handlers(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Move Blueprint handler registration to the application boundary."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "blueprint_error_handlers"
        and path in item.get("files", [])
    ]
    tree = _parsed(content)
    if tree is None or not decisions:
        return content
    changed = False

    for decision in decisions:
        if decision.get("handler_path") != path:
            continue
        for contract in decision.get("handlers", []):
            function = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == contract["function"]
            ), None)
            if function is None:
                continue
            decorators = [
                decorator for decorator in function.decorator_list
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {
                        "errorhandler", "app_errorhandler", "exception_handler",
                        "route", "get", "post", "put", "patch", "delete",
                    }
                )
            ]
            if decorators != function.decorator_list:
                function.decorator_list = decorators
                changed = True
            positional = function.args.args
            request = next(
                (argument for argument in positional if argument.arg == "request"),
                ast.arg(arg="request"),
            )
            if not positional or positional[0].arg != "request":
                positional[:] = [request, *(
                    argument for argument in positional if argument.arg != "request"
                )]
                changed = True
            if len(positional) == 1:
                required_at = len(positional) - len(function.args.defaults)
                positional.insert(
                    required_at, ast.arg(arg=contract.get("error_parameter") or "exc"),
                )
                changed = True

    factory_handlers = [
        handler
        for decision in decisions if path in decision.get("factory_files", [])
        for handler in decision.get("handlers", [])
    ]
    if factory_handlers:
        owned: dict[str, set[str]] = {}
        for contract in factory_handlers:
            module = contract["handler_path"].removesuffix(".py").replace("/", ".")
            owned.setdefault(module, set()).add(contract["function"])
        local_names = {contract["function"] for contract in factory_handlers}
        kept = []
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom):
                module = _resolve_module(statement.module, statement.level, path)
                if names := owned.get(module):
                    local_names.update(
                        alias.asname or alias.name
                        for alias in statement.names if alias.name in names
                    )
                    statement.names = [
                        alias for alias in statement.names if alias.name not in names
                    ]
                    changed = True
                    if not statement.names:
                        continue
            kept.append(statement)
        tree.body = kept

        class RemoveModelRegistrations(ast.NodeTransformer):
            changed = False

            def visit_Expr(self, node):  # noqa: N802
                self.generic_visit(node)
                call = node.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "add_exception_handler"
                    and len(call.args) >= 2
                    and isinstance(call.args[1], ast.Name)
                    and call.args[1].id in local_names
                ):
                    self.changed = True
                    return None
                return node

        registration_normalizer = RemoveModelRegistrations()
        tree = registration_normalizer.visit(tree)
        changed |= registration_normalizer.changed

    if factory_handlers and not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_portage_exception_response"
        for node in ast.walk(tree)
    ):
        factory = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "create_app"
        ), None)
        returns = [
            (index, statement)
            for index, statement in enumerate(factory.body if factory else [])
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
        ]
        if len(returns) == 1:
            return_index, returned = returns[0]
            app_name = returned.value.id
            setup = ast.parse(
                "from inspect import isawaitable as _portage_isawaitable\n"
                "from starlette.responses import Response as _PortageResponse\n"
                "from fastapi.responses import JSONResponse as _PortageJSONResponse\n"
                "async def _portage_exception_response(handler, request, exc, status_code):\n"
                "    result = handler(request, exc)\n"
                "    if _portage_isawaitable(result):\n"
                "        result = await result\n"
                "    body = result\n"
                "    if isinstance(result, tuple):\n"
                "        body, status_code = result[:2]\n"
                "    if isinstance(body, _PortageResponse):\n"
                "        body.status_code = status_code\n"
                "        return body\n"
                "    if isinstance(body, (dict, list)):\n"
                "        return _PortageJSONResponse(status_code=status_code, content=body)\n"
                "    return _PortageResponse(\n"
                "        content=b'' if body is None else body, status_code=status_code\n"
                "    )\n"
            ).body
            registrations = []
            for index, contract in enumerate(factory_handlers):
                module = contract["handler_path"].removesuffix(".py").replace("/", ".")
                handler_alias = f"_portage_source_handler_{index}"
                wrapper_name = f"_portage_blueprint_handler_{index}"
                registration = contract["registration"]
                imports = [ast.ImportFrom(
                    module=module,
                    names=[ast.alias(name=contract["function"], asname=handler_alias)],
                    level=0,
                )]
                if registration["kind"] == "status":
                    registered = ast.Constant(value=registration["value"])
                    default_status = ast.Constant(value=registration["value"])
                elif registration["kind"] == "builtin":
                    registered = ast.Name(id=registration["symbol"], ctx=ast.Load())
                    default_status = ast.Constant(value=500)
                else:
                    exception_alias = f"_portage_exception_{index}"
                    imports.append(ast.ImportFrom(
                        module=registration["module"],
                        names=[ast.alias(
                            name=registration["symbol"], asname=exception_alias,
                        )], level=0,
                    ))
                    registered = ast.Name(id=exception_alias, ctx=ast.Load())
                    default_status = ast.Call(
                        func=ast.Name(id="getattr", ctx=ast.Load()),
                        args=[
                            ast.Name(id="exc", ctx=ast.Load()),
                            ast.Constant(value="status_code"),
                            ast.Call(
                                func=ast.Name(id="getattr", ctx=ast.Load()),
                                args=[
                                    ast.Name(id="exc", ctx=ast.Load()),
                                    ast.Constant(value="code"), ast.Constant(value=500),
                                ], keywords=[],
                            ),
                        ], keywords=[],
                    )
                wrapper = ast.AsyncFunctionDef(
                    name=wrapper_name,
                    args=ast.arguments(
                        posonlyargs=[],
                        args=[ast.arg(arg="request"), ast.arg(arg="exc")],
                        kwonlyargs=[], kw_defaults=[], defaults=[],
                    ),
                    body=[ast.Return(value=ast.Await(value=ast.Call(
                        func=ast.Name(id="_portage_exception_response", ctx=ast.Load()),
                        args=[
                            ast.Name(id=handler_alias, ctx=ast.Load()),
                            ast.Name(id="request", ctx=ast.Load()),
                            ast.Name(id="exc", ctx=ast.Load()), default_status,
                        ], keywords=[],
                    )))],
                    decorator_list=[], returns=None, type_comment=None,
                )
                call = ast.Expr(value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id=app_name, ctx=ast.Load()),
                        attr="add_exception_handler", ctx=ast.Load(),
                    ),
                    args=[registered, ast.Name(id=wrapper_name, ctx=ast.Load())],
                    keywords=[],
                ))
                registrations.extend([*imports, wrapper, call])
            factory.body[return_index:return_index] = [*setup, *registrations]
            changed = True

    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_route_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Set mechanically-known FastAPI route names used by reverse lookup."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "route_names" and item.get("path") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    expected: dict[str, set[str]] = {}
    expected_by_path: dict[str, set[str]] = {}
    expected_by_shape: dict[str, set[str]] = {}
    prefixes: dict[str, set[str]] = {}
    prefixes_by_path: dict[str, set[str]] = {}
    prefixes_by_shape: dict[str, set[str]] = {}

    def route_shape(value: str) -> str:
        shaped = re.sub(r"\{[^}]+\}", "{}", value)
        return shaped.rstrip("/") or "/"

    for route in decision.get("routes", []):
        expected.setdefault(route["function"], set()).add(route["name"])
        if route.get("path"):
            expected_by_path.setdefault(route["path"], set()).add(route["name"])
            expected_by_shape.setdefault(
                route_shape(route["path"]), set(),
            ).add(route["name"])
        if route.get("prefix"):
            prefixes.setdefault(route["function"], set()).add(route["prefix"])
            if route.get("path"):
                prefixes_by_path.setdefault(route["path"], set()).add(route["prefix"])
                prefixes_by_shape.setdefault(
                    route_shape(route["path"]), set(),
                ).add(route["prefix"])
    changed = False
    router_prefixes: dict[str, set[str]] = {}
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for decorator in function.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {
                    "api_route", "get", "post", "put", "patch", "delete",
                    "options", "head",
                }
            ):
                continue
            route_path = decorator.args[0].value if (
                decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ) else ""
            path_names = expected_by_path.get(route_path, set()) or (
                expected_by_shape.get(route_shape(route_path), set())
                if route_path else set()
            )
            names = path_names or expected.get(function.name, set())
            if len(names) != 1:
                continue
            route_prefixes = prefixes_by_path.get(route_path, set()) or (
                prefixes_by_shape.get(route_shape(route_path), set())
                if route_path else prefixes.get(function.name, set())
            )
            if (
                len(route_prefixes) == 1
                and isinstance(decorator.func.value, ast.Name)
            ):
                router_prefixes.setdefault(
                    decorator.func.value.id, set(),
                ).update(route_prefixes)
            name = next(iter(names))
            keyword = next(
                (item for item in decorator.keywords if item.arg == "name"), None,
            )
            value = ast.Constant(name)
            if keyword is None:
                decorator.keywords.append(ast.keyword(arg="name", value=value))
            elif not (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value == name
            ):
                keyword.value = value
            else:
                continue
            changed = True
    for call in (
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_api_route"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Name)
    ):
        route_path = call.args[0].value if (
            isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ) else ""
        path_names = expected_by_path.get(route_path, set()) or (
            expected_by_shape.get(route_shape(route_path), set())
            if route_path else set()
        )
        names = path_names or expected.get(call.args[1].id, set())
        if len(names) != 1:
            continue
        route_prefixes = prefixes_by_path.get(route_path, set()) or (
            prefixes_by_shape.get(route_shape(route_path), set())
            if route_path else prefixes.get(call.args[1].id, set())
        )
        if len(route_prefixes) == 1 and isinstance(call.func.value, ast.Name):
            router_prefixes.setdefault(call.func.value.id, set()).update(route_prefixes)
        name = next(iter(names))
        keyword = next((item for item in call.keywords if item.arg == "name"), None)
        if keyword is None:
            call.keywords.append(ast.keyword(arg="name", value=ast.Constant(name)))
        elif isinstance(keyword.value, ast.Constant) and keyword.value.value == name:
            continue
        else:
            keyword.value = ast.Constant(name)
        changed = True
    for statement in tree.body:
        if not (
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1] == "APIRouter"
        ):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names = {
            target.id for target in targets
            if isinstance(target, ast.Name) and len(router_prefixes.get(target.id, set())) == 1
        }
        if len(names) != 1:
            continue
        prefix = next(iter(router_prefixes[next(iter(names))]))
        keyword = next(
            (item for item in statement.value.keywords if item.arg == "prefix"), None,
        )
        if keyword is None:
            statement.value.keywords.append(
                ast.keyword(arg="prefix", value=ast.Constant(prefix)),
            )
        elif isinstance(keyword.value, ast.Constant) and keyword.value.value == prefix:
            continue
        else:
            keyword.value = ast.Constant(prefix)
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_view_decorator_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Restore ``functools.wraps`` when the original view decorator used it."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "view_decorators" and item.get("path") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    functools_name = next((
        alias.asname or alias.name
        for statement in tree.body if isinstance(statement, ast.Import)
        for alias in statement.names if alias.name == "functools"
    ), None)
    wraps_name = next((
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "functools"
        for alias in statement.names if alias.name == "wraps"
    ), None)
    changed = False
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for contract in decision.get("decorators", []):
        function = functions.get(contract["function"])
        nested = [
            node for node in (function.body if function else [])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        returned = {
            node.value.id for node in (ast.walk(function) if function else ())
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        wrapper = next(
            (node for node in nested if node.name == contract["wrapper"]),
            next((node for node in nested if node.name in returned), None),
        )
        if wrapper is None:
            continue
        wrapped = any(
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).split(".")[-1] == "wraps"
            for decorator in wrapper.decorator_list
        )
        qualified_wraps = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "functools"
            and decorator.func.attr == "wraps"
            for decorator in wrapper.decorator_list
        )
        if qualified_wraps and functools_name is None:
            tree.body.insert(0, ast.Import(names=[ast.alias(name="functools")]))
            functools_name = "functools"
            changed = True
        if not wrapped:
            if functools_name is None and wraps_name is None:
                functools_name = "functools"
                insert_at = 1 if (
                    tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Constant)
                    and isinstance(tree.body[0].value.value, str)
                ) else 0
                while insert_at < len(tree.body) and isinstance(
                    tree.body[insert_at], (ast.Import, ast.ImportFrom)
                ):
                    insert_at += 1
                tree.body.insert(
                    insert_at, ast.Import(names=[ast.alias(name="functools")]),
                )
            decorator = (
                ast.Name(id=wraps_name, ctx=ast.Load()) if wraps_name else
                ast.Attribute(
                    value=ast.Name(id=functools_name, ctx=ast.Load()),
                    attr="wraps", ctx=ast.Load(),
                )
            )
            wrapper.decorator_list.insert(0, ast.Call(
                func=decorator,
                args=[ast.Name(id=contract["parameter"], ctx=ast.Load())],
                keywords=[],
            ))
            changed = True
        wrapper_parameters = {
            argument.arg for argument in [
                *wrapper.args.posonlyargs, *wrapper.args.args, *wrapper.args.kwonlyargs,
            ]
        }
        for call in (
            node for node in ast.walk(wrapper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == contract["parameter"]
        ):
            existing = {keyword.arg for keyword in call.keywords if keyword.arg}
            forwarded = [
                argument for argument in call.args
                if isinstance(argument, ast.Name)
                and argument.id in wrapper_parameters - existing
            ]
            if not forwarded:
                continue
            call.args = [argument for argument in call.args if argument not in forwarded]
            named = [
                ast.keyword(
                    arg=argument.id,
                    value=ast.Name(id=argument.id, ctx=ast.Load()),
                )
                for argument in forwarded
            ]
            splat = next(
                (index for index, keyword in enumerate(call.keywords)
                 if keyword.arg is None),
                len(call.keywords),
            )
            call.keywords[splat:splat] = named
            changed = True
        if wrapper.args.kwarg is not None:
            calls = [
                node for node in ast.walk(wrapper)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == contract["parameter"]
            ]
            for parameter in sorted(wrapper_parameters):
                forwarded = {
                    id(node)
                    for call in calls
                    for node in [
                        *(
                            argument for argument in call.args
                            if isinstance(argument, ast.Name)
                            and argument.id == parameter
                        ),
                        *(
                            keyword.value for keyword in call.keywords
                            if keyword.arg is not None
                            and isinstance(keyword.value, ast.Name)
                            and keyword.value.id == parameter
                        ),
                    ]
                }
                loads = {
                    id(node) for node in ast.walk(wrapper)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id == parameter
                }
                if not loads or loads - forwarded:
                    continue
                wrapper.args.posonlyargs = [
                    argument for argument in wrapper.args.posonlyargs
                    if argument.arg != parameter
                ]
                wrapper.args.args = [
                    argument for argument in wrapper.args.args
                    if argument.arg != parameter
                ]
                wrapper.args.kwonlyargs = [
                    argument for argument in wrapper.args.kwonlyargs
                    if argument.arg != parameter
                ]
                for call in calls:
                    call.args = [
                        argument for argument in call.args
                        if not isinstance(argument, ast.Name)
                        or argument.id != parameter
                    ]
                    call.keywords = [
                        keyword for keyword in call.keywords
                        if not (
                            keyword.arg is not None
                            and isinstance(keyword.value, ast.Name)
                            and keyword.value.id == parameter
                        )
                    ]
                changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"
