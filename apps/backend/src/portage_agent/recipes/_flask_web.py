"""Deterministic web-surface normalizers for Flask to FastAPI."""

from __future__ import annotations

import ast
import re
from copy import deepcopy
from http import HTTPStatus
from pathlib import PurePosixPath

from portage_agent.agent.nodes.common import _module_names, _resolve_module

from ._flask_analysis import (
    _FLASK_LOGIN_NAMES,
    _parsed,
)
from ._flask_runtime import _module_import_index


def _template_filter_contracts(files: dict[str, str]) -> list[dict]:
    """Resolve source-registered Jinja filters to importable callables."""
    contracts = []
    for path, source in files.items():
        tree = _parsed(source)
        if tree is None:
            continue
        imports = {
            alias.asname or alias.name: {
                "module": _resolve_module(statement.module, statement.level, path),
                "symbol": alias.name,
            }
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            for alias in statement.names if alias.name != "*"
        }
        local_functions = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def registration(
            value: ast.AST,
            name: str | None = None,
            imports: dict = imports,
            local_functions: set[str] = local_functions,
            path: str = path,
        ) -> dict | None:
            if not isinstance(value, ast.Name):
                return None
            resolved = imports.get(value.id)
            if resolved is None and value.id in local_functions:
                resolved = {
                    "module": path.removesuffix(".py").replace("/", "."),
                    "symbol": value.id,
                }
            return {**resolved, "name": name or resolved["symbol"]} if resolved else None

        for call in (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"add_app_template_filter", "add_template_filter"}
            and node.args
        ):
            named = next((
                keyword.value.value for keyword in call.keywords
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ), None)
            if named is None and len(call.args) > 1 and isinstance(
                call.args[1], ast.Constant,
            ) and isinstance(call.args[1].value, str):
                named = call.args[1].value
            if item := registration(call.args[0], named):
                contracts.append(item)
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for decorator in function.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {"app_template_filter", "template_filter"}
                ):
                    continue
                named = next((
                    keyword.value.value for keyword in decorator.keywords
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ), None)
                if named is None and decorator.args and isinstance(
                    decorator.args[0], ast.Constant,
                ) and isinstance(decorator.args[0].value, str):
                    named = decorator.args[0].value
                if item := registration(ast.Name(id=function.name), named):
                    contracts.append(item)
    return sorted(
        {(
            item["module"], item["symbol"], item["name"],
        ): item for item in contracts}.values(),
        key=lambda item: (item["name"], item["module"], item["symbol"]),
    )


def _realize_basic_auth_decoding(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Close model-invented Basic-auth decoder calls with the stdlib."""
    if not any(
        item.get("kind") == "basic_auth" and item.get("path") == path
        for item in (seam_plan or {}).get("decisions", {}).values()
    ):
        return content
    tree = _parsed(content)
    if tree is None:
        return content
    bound = {
        name
        for statement in tree.body
        for name in (
            [statement.name]
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [alias.asname or alias.name.split(".")[0] for alias in statement.names]
            if isinstance(statement, (ast.Import, ast.ImportFrom))
            else [
                target.id for target in statement.targets
                if isinstance(target, ast.Name)
            ]
            if isinstance(statement, ast.Assign)
            else []
        )
    }
    missing = sorted({
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id not in bound
        and re.search(
            r"(?:decode|parse).*basic.*auth|basic.*auth.*(?:decode|parse)",
            node.func.id, re.IGNORECASE,
        )
    })
    if not missing:
        return content
    imported_alias = next((
        alias for statement in tree.body if isinstance(statement, ast.Import)
        for alias in statement.names if alias.name == "base64"
    ), None)
    if imported_alias is None:
        index = int(bool(
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ))
        while (
            index < len(tree.body)
            and isinstance(tree.body[index], ast.ImportFrom)
            and tree.body[index].module == "__future__"
        ):
            index += 1
        tree.body.insert(index, ast.Import(
            names=[ast.alias(name="base64", asname="_portage_base64")],
        ))
        base64_name = "_portage_base64"
    else:
        base64_name = imported_alias.asname or "base64"
    helpers = []
    for name in missing:
        helpers.extend(ast.parse(
            f"def {name}(value):\n"
            "    scheme, token = value.split(' ', 1)\n"
            "    if scheme.lower() != 'basic':\n"
            "        raise ValueError('invalid Basic authorization')\n"
            f"    decoded = {base64_name}.b64decode(token, validate=True).decode('utf-8')\n"
            "    if ':' not in decoded:\n"
            "        raise ValueError('invalid Basic authorization')\n"
            "    return decoded.split(':', 1)\n"
        ).body)
    tree.body.extend(helpers)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_template_directory(content: str) -> str:
    """Keep relative template roots package-relative, matching Flask semantics."""
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    for call in (
        node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and ast.unparse(node.func).split(".")[-1] == "Jinja2Templates"
    ):
        keyword = next((item for item in call.keywords if item.arg == "directory"), None)
        if not (
            keyword is not None and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
            and not PurePosixPath(keyword.value.value).is_absolute()
        ):
            continue
        expression: ast.expr = ast.Attribute(
            value=ast.Call(
                func=ast.Attribute(
                    value=ast.Call(
                        func=ast.Name(id="Path", ctx=ast.Load()),
                        args=[ast.Name(id="__file__", ctx=ast.Load())], keywords=[],
                    ),
                    attr="resolve", ctx=ast.Load(),
                ),
                args=[], keywords=[],
            ),
            attr="parent", ctx=ast.Load(),
        )
        for part in PurePosixPath(keyword.value.value).parts:
            expression = ast.BinOp(
                left=expression, op=ast.Div(), right=ast.Constant(part),
            )
        keyword.value = ast.Call(
            func=ast.Name(id="str", ctx=ast.Load()), args=[expression], keywords=[],
        )
        changed = True
    if not changed:
        return content
    pathlib_import = next((
        statement for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "pathlib"
    ), None)
    if pathlib_import is None:
        index = int(bool(
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ))
        while (
            index < len(tree.body) and isinstance(tree.body[index], ast.ImportFrom)
            and tree.body[index].module == "__future__"
        ):
            index += 1
        tree.body.insert(index, ast.ImportFrom(
            module="pathlib", names=[ast.alias(name="Path")], level=0,
        ))
    elif not any(alias.name == "Path" for alias in pathlib_import.names):
        pathlib_import.names.append(ast.alias(name="Path"))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_flask_string_responses(content: str) -> str:
    """Keep Flask's HTML response semantics for computed string route results."""
    tree = _parsed(content)
    if tree is None:
        return content
    route_functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {
                "api_route", "delete", "get", "head", "options", "patch", "post", "put",
            }
            for decorator in node.decorator_list
        )
    ]
    if not route_functions:
        return content

    class WrapReturns(ast.NodeTransformer):
        changed = False

        def visit_FunctionDef(self, node):  # noqa: N802
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Return(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if node.value is None or (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_portage_flask_response"
            ):
                return node
            self.changed = True
            node.value = ast.Call(
                func=ast.Name(id="_portage_flask_response", ctx=ast.Load()),
                args=[node.value], keywords=[],
            )
            return node

    wrapper = WrapReturns()
    for function in route_functions:
        function.body = [wrapper.visit(statement) for statement in function.body]
    if not wrapper.changed:
        return content
    response_import = next((
        statement for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"fastapi.responses", "starlette.responses"}
    ), None)
    if response_import is None:
        tree.body.insert(_module_import_index(tree), ast.ImportFrom(
            module="fastapi.responses", names=[ast.alias(name="HTMLResponse")], level=0,
        ))
    elif not any(alias.name == "HTMLResponse" for alias in response_import.names):
        response_import.names.append(ast.alias(name="HTMLResponse"))
    tree.body.append(ast.parse(
        "def _portage_flask_response(value):\n"
        "    return HTMLResponse(value) if isinstance(value, str) else value\n"
    ).body[0])
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_async_request_dependencies(content: str) -> str:
    """Await Starlette request-body methods inside FastAPI dependencies."""
    tree = _parsed(content)
    if tree is None:
        return content
    dependency_names = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).split(".")[-1] == "Depends"
        and node.args and isinstance(node.args[0], ast.Name)
    }
    if not dependency_names:
        return content

    class AwaitRequest(ast.NodeTransformer):
        changed = False

        def visit_Await(self, node):  # noqa: N802
            return node

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"body", "form", "json"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "request"
            ):
                return node
            self.changed = True
            return ast.Await(value=node)

    changed = False
    for index, function in enumerate(tree.body):
        if not (
            isinstance(function, ast.FunctionDef)
            and function.name in dependency_names
        ):
            continue
        awaiter = AwaitRequest()
        function = awaiter.visit(function)
        if not awaiter.changed:
            continue
        tree.body[index] = ast.copy_location(ast.AsyncFunctionDef(**{
            field: getattr(function, field) for field in function._fields
        }), function)
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


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
    imported_instances = {
        alias.asname or alias.name
        for statement in tree.body if isinstance(statement, ast.ImportFrom)
        for alias in statement.names if alias.name == "templates"
    }
    instances.update(imported_instances)
    instances.update(
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Name)
        and statement.value.id in imported_instances
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    )
    if not instances:
        return content
    direct_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"starlette.responses", "fastapi.responses"}
        for alias in statement.names if alias.name == "TemplateResponse"
    }

    def request_argument(function):
        return next((
            argument for argument in [
                *function.args.posonlyargs, *function.args.args,
                *function.args.kwonlyargs,
            ]
            if argument.arg == "request"
            or argument.annotation is not None
            and ast.unparse(argument.annotation).split(".")[-1] == "Request"
        ), None)

    wired = False
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def own_calls(function, name):
        calls = []

        class Calls(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # noqa: N802
                return None

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):  # noqa: N802
                if isinstance(node.func, ast.Name) and node.func.id == name:
                    calls.append(node)
                self.generic_visit(node)

        visitor = Calls()
        for statement in function.body:
            visitor.visit(statement)
        return calls

    for helper in functions:
        if request_argument(helper) is not None or not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TemplateResponse"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in instances
            for node in ast.walk(helper)
        ):
            continue
        call_sites = []
        annotation = None
        for caller in functions:
            if caller is helper:
                continue
            calls = own_calls(caller, helper.name)
            if not calls:
                continue
            argument = request_argument(caller)
            if argument is None:
                call_sites = []
                break
            annotation = annotation or argument.annotation
            call_sites.extend((call, argument.arg) for call in calls)
        if not call_sites:
            continue
        helper.args.args.append(ast.arg(arg="request", annotation=annotation))
        for call, name in call_sites:
            call.args.append(ast.Name(id=name, ctx=ast.Load()))
        wired = True

    class Upgrade(ast.NodeTransformer):
        def __init__(self):
            self.requests: list[str] = []
            self.changed = False

        def _function(self, node):
            argument = request_argument(node)
            self.requests.append(argument.arg if argument else "")
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
    upgrade.changed = wired
    tree = upgrade.visit(tree)
    if upgrade.changed:
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

    json_response_names = {
        alias.asname or alias.name
        for statement in tree.body if isinstance(statement, ast.ImportFrom)
        and statement.module in {"fastapi.responses", "starlette.responses"}
        for alias in statement.names if alias.name == "JSONResponse"
    }
    template_helpers = {
        function.name: function
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (returns := [
            statement.value for statement in function.body
            if isinstance(statement, ast.Return) and statement.value is not None
        ])
        and all(
            isinstance(value, ast.Call)
            and ast.unparse(value.func).split(".")[-1]
            in {"TemplateResponse", "render_template"}
            for value in returns
        )
    }

    class FlattenTemplateJSON(ast.NodeTransformer):
        changed = False

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if not (
                isinstance(node.func, ast.Name) and node.func.id in json_response_names
            ):
                return node
            content = node.args[0] if node.args else next((
                keyword.value for keyword in node.keywords if keyword.arg == "content"
            ), None)
            if not isinstance(content, ast.Call):
                return node
            template_call = (
                isinstance(content.func, ast.Attribute)
                and content.func.attr == "TemplateResponse"
                and isinstance(content.func.value, ast.Name)
                and content.func.value.id in instances
            )
            helper_call = (
                isinstance(content.func, ast.Name)
                and content.func.id in template_helpers
            )
            if not (template_call or helper_call):
                return node
            outer_status = next((
                keyword for keyword in node.keywords if keyword.arg == "status_code"
            ), None)
            if outer_status and helper_call:
                helper = template_helpers[content.func.id]
                for returned in (
                    statement for statement in helper.body
                    if isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Call)
                ):
                    if not any(
                        keyword.arg == "status_code"
                        for keyword in returned.value.keywords
                    ):
                        returned.value.keywords.append(deepcopy(outer_status))
            elif outer_status and not any(
                keyword.arg == "status_code" for keyword in content.keywords
            ):
                content.keywords.append(deepcopy(outer_status))
            self.changed = True
            return content

    flatten_json = FlattenTemplateJSON()
    tree = flatten_json.visit(tree)
    if flatten_json.changed:
        for statement in list(tree.body):
            if not (
                isinstance(statement, ast.ImportFrom)
                and statement.module in {"fastapi.responses", "starlette.responses"}
            ):
                continue
            statement.names = [
                alias for alias in statement.names if alias.name != "JSONResponse"
            ]
            if not statement.names:
                tree.body.remove(statement)

    response_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"fastapi", "fastapi.responses", "starlette.responses"}
        for alias in statement.names if alias.name in {"Response", "HTMLResponse"}
    }

    class FlattenResponse(ast.NodeTransformer):
        changed = False

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id in response_names
                and len(node.args) == 1
                and all(keyword.arg == "media_type" for keyword in node.keywords)
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id in template_helpers
            ):
                return node
            self.changed = True
            return node.args[0]

    flatten = FlattenResponse()
    tree = flatten.visit(tree)
    if not (upgrade.changed or flatten_json.changed or flatten.changed):
        return content
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


def _realize_route_response_statuses(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Restore frozen non-success statuses on translated route returns."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "route_response_statuses" and item.get("path") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    changed = False

    def exception_names(node: ast.AST | None) -> set[str]:
        if isinstance(node, ast.Tuple):
            return {name for item in node.elts for name in exception_names(item)}
        if isinstance(node, (ast.Name, ast.Attribute)):
            return {ast.unparse(node).split(".")[-1]}
        return set()

    def apply_status(returned: ast.Return, status: int, scope: ast.AST) -> None:
        nonlocal changed
        value = returned.value
        if value is None:
            return
        if isinstance(value, ast.Name) and (
            value.id == "_portage_response"
            or any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == value.id
                    and target.attr == "status_code"
                    for target in node.targets
                )
                for node in ast.walk(scope)
            )
        ):
            return
        if (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "_portage_flask_response" and len(value.args) == 1
        ):
            apply_status(ast.Return(value=value.args[0]), status, scope)
            return
        if isinstance(value, ast.Call) and ast.unparse(value.func).split(".")[-1] in {
            "HTMLResponse", "JSONResponse", "PlainTextResponse", "Response",
            "TemplateResponse", "render_template",
        }:
            keyword = next((item for item in value.keywords if item.arg == "status_code"), None)
            if keyword is None:
                value.keywords.append(ast.keyword(
                    arg="status_code", value=ast.Constant(status),
                ))
            else:
                keyword.value = ast.Constant(status)
            changed = True
            return
        if (
            isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and (helper := functions.get(value.func.id)) is not None
        ):
            helper_returns = [
                node for statement in helper.body for node in ast.walk(statement)
                if isinstance(node, ast.Return) and node.value is not None
            ]
            if helper_returns and all(
                isinstance(item.value, ast.Call)
                and ast.unparse(item.value.func).split(".")[-1]
                in {"TemplateResponse", "render_template"}
                for item in helper_returns
            ):
                for item in helper_returns:
                    apply_status(item, status, helper)
                return
        returned.value = ast.Call(
            func=ast.Name(id="JSONResponse", ctx=ast.Load()), args=[],
            keywords=[
                ast.keyword(arg="content", value=value),
                ast.keyword(arg="status_code", value=ast.Constant(status)),
            ],
        )
        changed = True

    for route in decision.get("routes", []):
        function = functions.get(route["function"])
        if function is None:
            continue
        for contract in route.get("handlers", []):
            for handler in (
                node for node in ast.walk(function) if isinstance(node, ast.ExceptHandler)
                and exception_names(node.type) & set(contract["exceptions"])
            ):
                for returned in (
                    node for statement in handler.body for node in ast.walk(statement)
                    if isinstance(node, ast.Return)
                ):
                    apply_status(returned, contract["status_code"], handler)
        for returned in (
            node for node in ast.walk(function) if isinstance(node, ast.Return)
            and node.value is not None
        ):
            literals = {
                node.value for node in ast.walk(returned.value)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            matches = [
                item for item in route.get("returns", [])
                if literals & set(item["literals"])
            ]
            if len(matches) == 1:
                apply_status(returned, matches[0]["status_code"], function)

    envelope_keys: dict[int, set[str]] = {}
    for route in decision.get("routes", []):
        for contract in [*route.get("handlers", []), *route.get("returns", [])]:
            if key := contract.get("content_key"):
                envelope_keys.setdefault(contract["status_code"], set()).add(key)
    envelopes = {
        status: next(iter(keys)) for status, keys in envelope_keys.items()
        if len(keys) == 1
    }
    exception_contracts: dict[str, set[tuple[int, str]]] = {}
    for route in decision.get("routes", []):
        for contract in route.get("handlers", []):
            if key := contract.get("content_key"):
                for exception in contract.get("exceptions", []):
                    exception_contracts.setdefault(exception, set()).add((
                        contract["status_code"], key,
                    ))
    exception_envelopes = {
        exception: next(iter(contracts))
        for exception, contracts in exception_contracts.items()
        if len(contracts) == 1
    }
    http_exception_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        for alias in statement.names if alias.name == "HTTPException"
    }

    def status_value(call: ast.Call) -> int | None:
        value = next(
            (item.value for item in call.keywords if item.arg == "status_code"),
            call.args[0] if call.args else None,
        )
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return value.value
        if isinstance(value, ast.Attribute) and value.attr in HTTPStatus.__members__:
            return HTTPStatus[value.attr].value
        return None

    factory = next((
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "create_app"
    ), None)
    app_name = next((
        node.value.id for node in reversed(factory.body if factory else [])
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    ), None)
    if factory is None or app_name is None:
        envelopes = {}
        exception_envelopes = {}

    dependency_names = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] == "Depends"
            and node.args
        ):
            continue
        dependency = node.args[0]
        if isinstance(dependency, ast.Name):
            dependency_names.add(dependency.id)
        elif isinstance(dependency, ast.Call) and isinstance(dependency.func, ast.Name):
            dependency_names.add(dependency.func.id)

    source_statuses = set()

    class DependencyExceptions(ast.NodeTransformer):
        def visit_Raise(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if not (
                isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id in exception_envelopes
            ):
                return node
            status, _ = exception_envelopes[node.exc.func.id]
            detail = node.exc.args[0] if node.exc.args else ast.Constant(node.exc.func.id)
            node.exc = ast.Call(
                func=ast.Name(id="_PortageEnvelopeHTTPException", ctx=ast.Load()),
                args=[], keywords=[
                    ast.keyword(arg="status_code", value=ast.Constant(status)),
                    ast.keyword(arg="detail", value=detail),
                ],
            )
            source_statuses.add(status)
            return node

    dependency_normalizer = DependencyExceptions()
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in dependency_names
    ):
        dependency_normalizer.visit(function)

    if source_statuses and not http_exception_names:
        fastapi_import = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        ), None)
        if fastapi_import is None:
            tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                module="fastapi", names=[ast.alias(name="HTTPException")], level=0,
            ))
        else:
            fastapi_import.names.append(ast.alias(name="HTTPException"))
        http_exception_names.add("HTTPException")

    class EnvelopeExceptions(ast.NodeTransformer):
        statuses: set[int] = set()

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            status = status_value(node)
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id in http_exception_names
                and status in envelopes
            ):
                return node
            node.func = ast.Name(
                id="_PortageEnvelopeHTTPException", ctx=ast.Load(),
            )
            self.statuses.add(status)
            return node

    envelope_normalizer = EnvelopeExceptions()
    envelope_normalizer.statuses.update(source_statuses)
    tree = envelope_normalizer.visit(tree)
    if envelope_normalizer.statuses and not any(
        isinstance(node, ast.ClassDef)
        and node.name == "_PortageEnvelopeHTTPException"
        for node in tree.body
    ):
        http_exception_name = sorted(http_exception_names)[0]
        exception_class = ast.parse(
            f"class _PortageEnvelopeHTTPException({http_exception_name}):\n"
            "    pass"
        ).body[0]
        index = tree.body.index(factory)
        tree.body.insert(index, exception_class)
        mapping = {
            status: envelopes[status]
            for status in sorted(envelope_normalizer.statuses)
        }
        handler = ast.parse(
            f"@{app_name}.exception_handler(_PortageEnvelopeHTTPException)\n"
            "async def _portage_http_exception_envelope(_request, exc):\n"
            f"    key = {mapping!r}[exc.status_code]\n"
            "    return JSONResponse(content={key: exc.detail}, "
            "status_code=exc.status_code, headers=exc.headers)"
        ).body[0]
        return_index = next(
            index for index in range(len(factory.body) - 1, -1, -1)
            if isinstance(factory.body[index], ast.Return)
        )
        factory.body.insert(return_index, handler)
        changed = True
    if not changed:
        return content
    imported = any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "fastapi.responses"
        and any(alias.name == "JSONResponse" for alias in statement.names)
        for statement in tree.body
    )
    if not imported:
        index = int(bool(
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ))
        while (
            index < len(tree.body) and isinstance(tree.body[index], ast.ImportFrom)
            and tree.body[index].module == "__future__"
        ):
            index += 1
        tree.body.insert(index, ast.ImportFrom(
            module="fastapi.responses", names=[ast.alias(name="JSONResponse")], level=0,
        ))
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
        and not any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {
                "api_route", "delete", "get", "head", "options", "patch", "post", "put",
            }
            for decorator in node.decorator_list
        )
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
            tree.body.insert(_module_import_index(tree), imported)
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

    ambient_provider = next((
        providers[0]
        for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "ambient_context_runtime"
        and path in item.get("files", [])
        and len(providers := item.get("runtime_providers", [])) == 1
    ), "")
    ambient_name = ""
    if ambient_provider and any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        and call.func.id in local_names
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not any(
            argument.arg == "request"
            or argument.annotation is not None
            and ast.unparse(argument.annotation).split(".")[-1] == "Request"
            for argument in [
                *function.args.posonlyargs, *function.args.args,
                *function.args.kwonlyargs,
            ]
        )
        for call in ast.walk(function)
    ):
        imported = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(ambient_provider)
        ), None)
        if imported is None:
            imported = ast.ImportFrom(
                module=ambient_provider.removesuffix(".py").replace("/", "."),
                names=[], level=0,
            )
            tree.body.insert(_module_import_index(tree), imported)
        alias = next((
            item for item in imported.names if item.name == "g"
        ), None)
        if alias is None:
            imported.names.append(ast.alias(name="g"))
            imports_changed = True
            ambient_name = "g"
        else:
            ambient_name = alias.asname or alias.name

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
        tree.body.insert(_module_import_index(tree), ast.ImportFrom(
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
            for index, argument in enumerate(node.args):
                if (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Name)
                    and argument.func.id in local_names
                ):
                    node.args[index] = ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=argument, attr="body", ctx=ast.Load(),
                            ),
                            attr="decode", ctx=ast.Load(),
                        ),
                        args=[], keywords=[],
                    )
                    self.changed = True
            for keyword in node.keywords:
                if (
                    isinstance(keyword.value, ast.Call)
                    and isinstance(keyword.value.func, ast.Name)
                    and keyword.value.func.id in local_names
                ):
                    keyword.value = ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=keyword.value, attr="body", ctx=ast.Load(),
                            ),
                            attr="decode", ctx=ast.Load(),
                        ),
                        args=[], keywords=[],
                    )
                    self.changed = True
            request = self.requests[-1] if self.requests else ""
            is_template_call = (
                isinstance(node.func, ast.Name) and node.func.id in local_names
            )
            without_request_types = [
                argument for argument in node.args
                if not (
                    isinstance(argument, ast.Name) and argument.id in request_types
                    or isinstance(argument, ast.Call)
                    and ast.unparse(argument.func).split(".")[-1] in request_types
                )
            ]
            if is_template_call and len(without_request_types) != len(node.args):
                node.args = without_request_types
                self.changed = True
            if request:
                while (
                    len(node.args) >= 2
                    and all(
                        isinstance(argument, ast.Name) and argument.id == request
                        for argument in node.args[:2]
                    )
                ):
                    node.args.pop(1)
                    self.changed = True
            if (
                request and len(local_names) == 1 and len(node.args) >= 2
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == request
            ):
                context = node.args[2] if len(node.args) > 2 else None
                keywords = [
                    keyword for keyword in node.keywords
                    if keyword.arg != "request"
                ]
                if not (
                    context is None
                    or isinstance(context, ast.Dict) and not context.keys
                ):
                    keywords.append(ast.keyword(arg=None, value=context))
                self.changed = True
                return ast.Call(
                    func=ast.Name(id=next(iter(local_names)), ctx=ast.Load()),
                    args=[node.args[0], node.args[1]], keywords=keywords,
                )
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
            elif (
                not request and ambient_name and isinstance(node.func, ast.Name)
                and node.func.id in local_names
            ):
                ambient_request = ast.Attribute(
                    value=ast.Name(id=ambient_name, ctx=ast.Load()),
                    attr="request", ctx=ast.Load(),
                )
                if (
                    node.args and ast.dump(node.args[0]) == ast.dump(ambient_request)
                ):
                    while (
                        len(node.args) >= 2
                        and ast.dump(node.args[1]) == ast.dump(ambient_request)
                    ):
                        node.args.pop(1)
                        self.changed = True
                else:
                    node.args.insert(0, ambient_request)
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
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    provider_path = decision.get("authentication_provider", "")
    if "current_user" in decision.get("context_globals", []) and provider_path:
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
    existing_filters = {
        node.slice.value for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and ast.unparse(node.value) == "templates.env.filters"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    for index, item in enumerate(decision.get("filters", [])):
        if item["name"] in existing_filters:
            continue
        alias = f"_portage_template_filter_{index}"
        if not any(
            isinstance(statement, ast.ImportFrom)
            and statement.module == item["module"]
            and any(
                imported.name == item["symbol"] and imported.asname == alias
                for imported in statement.names
            )
            for statement in tree.body
        ):
            tree.body.append(ast.ImportFrom(
                module=item["module"],
                names=[ast.alias(name=item["symbol"], asname=alias)],
                level=0,
            ))
        tree.body.append(ast.Assign(
            targets=[ast.Subscript(
                value=ast.Attribute(
                    value=ast.Attribute(
                        value=ast.Name(id="templates", ctx=ast.Load()),
                        attr="env", ctx=ast.Load(),
                    ),
                    attr="filters", ctx=ast.Load(),
                ),
                slice=ast.Constant(item["name"]), ctx=ast.Store(),
            )],
            value=ast.Name(id=alias, ctx=ast.Load()),
        ))
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


def _normalize_url_string_methods(content: str) -> str:
    """Coerce Starlette URL objects before applying Python string trimming."""
    tree = _parsed(content)
    if tree is None:
        return content

    class CoerceURL(ast.NodeTransformer):
        changed = False

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"strip", "lstrip", "rstrip"}
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Attribute)
                and node.func.value.func.attr == "url_for"
            ):
                return node
            node.func.value = ast.Call(
                func=ast.Name(id="str", ctx=ast.Load()),
                args=[node.func.value], keywords=[],
            )
            self.changed = True
            return node

    normalizer = CoerceURL()
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
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for contract in decision.get("decorators", []):
        function = next((
            node for node in functions if node.name == contract["function"]
        ), None)
        container = next((
            node for node in (function.body if function else [])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == contract.get("container")
        ), None) if contract.get("container") else function
        nested = [
            node for node in (container.body if container else [])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        returned = {
            node.value.id for node in (ast.walk(container) if container else ())
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
            tree.body.insert(
                _module_import_index(tree),
                ast.Import(names=[ast.alias(name="functools")]),
            )
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
        wrapped_calls = [
            node for node in ast.walk(wrapper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == contract["parameter"]
        ]
        injected = max((
            sum(
                not isinstance(argument, ast.Starred)
                and not (
                    isinstance(argument, ast.Name)
                    and argument.id in wrapper_parameters
                )
                for argument in call.args
            )
            for call in wrapped_calls
        ), default=0)
        if injected and container is not None and not any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == wrapper.name
                and target.attr == "__signature__"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
            for node in container.body
        ):
            inspect_name = next((
                alias.asname or alias.name
                for statement in tree.body if isinstance(statement, ast.Import)
                for alias in statement.names if alias.name == "inspect"
            ), None)
            if inspect_name is None:
                inspect_name = "inspect"
                insert_at = int(bool(
                    tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Constant)
                    and isinstance(tree.body[0].value.value, str)
                ))
                while (
                    insert_at < len(tree.body)
                    and isinstance(tree.body[insert_at], ast.ImportFrom)
                    and tree.body[insert_at].module == "__future__"
                ):
                    insert_at += 1
                tree.body.insert(insert_at, ast.Import(names=[ast.alias(name="inspect")]))
            wrapper_index = container.body.index(wrapper)
            parameters_name = f"_portage_{wrapper.name}_parameters"
            signature_statements = ast.parse(
                f"{parameters_name} = list({inspect_name}.signature("
                f"{contract['parameter']}).parameters.values())[{injected}:]\n"
                f"{wrapper.name}.__signature__ = {inspect_name}.signature("
                f"{contract['parameter']}).replace(parameters={parameters_name})"
            ).body
            request_argument = next((
                argument for argument in [
                    *wrapper.args.posonlyargs, *wrapper.args.args,
                ]
                if argument.arg == "request" and argument.annotation is not None
            ), None)
            if request_argument is not None:
                signature_statements[1:1] = ast.parse(
                    f"if not any(parameter.name == 'request' "
                    f"for parameter in {parameters_name}):\n"
                    f"    {parameters_name}.insert(0, {inspect_name}.Parameter("
                    f"'request', {inspect_name}.Parameter.POSITIONAL_OR_KEYWORD, "
                    f"annotation={ast.unparse(request_argument.annotation)}))"
                ).body
            container.body[wrapper_index + 1:wrapper_index + 1] = signature_statements
            changed = True
        for call in (
            node for node in wrapped_calls
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
