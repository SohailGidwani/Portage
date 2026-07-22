"""Deterministic runtime/provider normalizers for Flask to FastAPI."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import PurePosixPath

from portage_agent.agent.nodes.common import _module_names, _resolve_module

from ._flask_analysis import (
    _parsed,
)


def _realize_ambient_request_binding(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Keep the active Request in the shared ambient-context mapping."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "ambient_context_runtime"
        and path in item.get("runtime_providers", [])
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content

    changed = False
    runtime_classes = set(decision.get("runtime_classes", {}).get(path, []))
    for cls in (
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in runtime_classes
    ):
        for function in (
            node for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"dispatch", "__call__"}
        ):
            positional = [*function.args.posonlyargs, *function.args.args]
            request_name = next(
                (argument.arg for argument in positional if argument.arg != "self"), "",
            )
            if not request_name:
                continue
            for mapping in (
                node for node in ast.walk(function) if isinstance(node, ast.Dict)
            ):
                pairs = list(zip(mapping.keys, mapping.values, strict=True))
                if not any(
                    isinstance(key, ast.Constant) and key.value == "session"
                    and isinstance(value, ast.Attribute) and value.attr == "session"
                    and isinstance(value.value, ast.Name)
                    and value.value.id == request_name
                    for key, value in pairs
                ) or any(
                    isinstance(key, ast.Constant) and key.value == "request"
                    for key in mapping.keys
                ):
                    continue
                mapping.keys.append(ast.Constant("request"))
                mapping.values.append(ast.Name(id=request_name, ctx=ast.Load()))
                changed = True

    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_decorated_provider_protocols(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Fill only statically missing direct-decorator members on source providers."""
    protocols = [
        decision
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "provider_protocol"
        and decision.get("provider") == path
    ]
    tree = _parsed(content)
    if tree is None or not protocols:
        return content

    changed = False
    for protocol in protocols:
        symbol = protocol["symbol"]
        assignment = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        if assignment is None:
            continue
        existing = {
            target.attr
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name) and target.value.id == symbol
        }
        value = assignment.value
        class_name = (
            value.func.id
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            else ""
        )
        provider_class = next((
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ), None)
        if provider_class is not None:
            existing.update(
                node.name for node in provider_class.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        decorator_members = set(protocol.get("decorator_members", []))
        callable_members = set(protocol.get("callable_members", []))
        attribute_members = set(protocol.get("attribute_members", []))
        attribute_values = protocol.get("attribute_values", {})
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == symbol
                    and target.attr in attribute_values
                ):
                    statement.value = ast.Constant(value=attribute_values[target.attr])
                    changed = True
        for callback in protocol.get("callbacks", []):
            original = _parsed(callback["source"])
            if original is None or len(original.body) != 1:
                continue
            replacement = original.body[0]
            existing_callback = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == callback["function"]
            ), None)
            if existing_callback is None:
                tree.body.insert(tree.body.index(assignment) + 1, replacement)
            else:
                tree.body[tree.body.index(existing_callback)] = replacement
            changed = True
        missing = sorted((decorator_members | callable_members) - existing)
        missing_attributes = sorted(attribute_members - existing)
        if not missing and not missing_attributes:
            continue

        if isinstance(value, ast.Call):
            insertion = tree.body.index(assignment) + 1
            additions: list[ast.stmt] = []
            for member in sorted(decorator_members & set(missing)):
                helper = f"_portage_{symbol}_{member}"
                additions.extend([
                    ast.FunctionDef(
                        name=helper,
                        args=ast.arguments(
                            posonlyargs=[], args=[ast.arg(arg="callback")],
                            kwonlyargs=[], kw_defaults=[], defaults=[],
                        ),
                        body=[ast.Return(value=ast.Name(id="callback", ctx=ast.Load()))],
                        decorator_list=[], returns=None, type_comment=None,
                    ),
                    ast.Assign(
                        targets=[ast.Attribute(
                            value=ast.Name(id=symbol, ctx=ast.Load()),
                            attr=member, ctx=ast.Store(),
                        )],
                        value=ast.Name(id=helper, ctx=ast.Load()),
                    ),
                ])
            for member in sorted(callable_members - decorator_members - existing):
                additions.append(ast.FunctionDef(
                    name=f"_portage_{symbol}_{member}",
                    args=ast.arguments(
                        posonlyargs=[], args=[], vararg=ast.arg(arg="args"),
                        kwonlyargs=[], kw_defaults=[], kwarg=ast.arg(arg="kwargs"),
                        defaults=[],
                    ),
                    body=[ast.Return(value=ast.Constant(value=None))],
                    decorator_list=[], returns=None, type_comment=None,
                ))
                additions.append(ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id=symbol, ctx=ast.Load()),
                        attr=member, ctx=ast.Store(),
                    )],
                    value=ast.Name(
                        id=f"_portage_{symbol}_{member}", ctx=ast.Load(),
                    ),
                ))
            additions.extend(
                ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id=symbol, ctx=ast.Load()),
                        attr=member, ctx=ast.Store(),
                    )],
                    value=ast.Constant(value=attribute_values.get(member)),
                )
                for member in missing_attributes
            )
            tree.body[insertion:insertion] = additions
        else:
            helper_class = f"_Portage{''.join(part.title() for part in symbol.split('_'))}Protocol"
            class_node = ast.ClassDef(
                name=helper_class, bases=[], keywords=[], decorator_list=[],
                body=[
                    ast.FunctionDef(
                        name=member,
                        args=ast.arguments(
                            posonlyargs=[],
                            args=[ast.arg(arg="self"), ast.arg(arg="callback")],
                            kwonlyargs=[], kw_defaults=[], defaults=[],
                        ),
                        body=[ast.Return(value=ast.Name(id="callback", ctx=ast.Load()))],
                        decorator_list=[], returns=None, type_comment=None,
                    )
                    for member in sorted(decorator_members & set(missing))
                ] + [
                    ast.FunctionDef(
                        name=member,
                        args=ast.arguments(
                            posonlyargs=[], args=[ast.arg(arg="self")],
                            vararg=ast.arg(arg="args"), kwonlyargs=[],
                            kw_defaults=[], kwarg=ast.arg(arg="kwargs"), defaults=[],
                        ),
                        body=[ast.Return(value=ast.Constant(value=None))],
                        decorator_list=[], returns=None, type_comment=None,
                    )
                    for member in sorted(callable_members - decorator_members - existing)
                ] + [
                    ast.Assign(
                        targets=[ast.Name(id=member, ctx=ast.Store())],
                        value=ast.Constant(value=attribute_values.get(member)),
                    )
                    for member in missing_attributes
                ],
            )
            insertion = tree.body.index(assignment)
            tree.body.insert(insertion, class_node)
            assignment.value = ast.Call(
                func=ast.Name(id=helper_class, ctx=ast.Load()), args=[], keywords=[],
            )
        changed = True

    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_dynamic_instance_exports(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Make model-generated ``type`` facades honor frozen instance/decorator shape."""
    contracts = {
        item["symbol"]: item
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "application_factory"
        and decision.get("factory") == path
        for item in decision.get("instance_exports", [])
    }
    if not contracts:
        return content
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    receiver_names = {"self", "cls", "_self", "_", "instance"}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        symbol = next((
            target.id for target in targets
            if isinstance(target, ast.Name) and target.id in contracts
        ), "")
        if not symbol:
            continue
        value = statement.value
        constructor = (
            value if isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name) and value.func.id == "type"
            else value.func if isinstance(value, ast.Call)
            and isinstance(value.func, ast.Call)
            and isinstance(value.func.func, ast.Name) and value.func.func.id == "type"
            else None
        )
        if not (
            isinstance(constructor, ast.Call) and len(constructor.args) == 3
            and isinstance(constructor.args[2], ast.Dict)
        ):
            continue
        if constructor is value:
            statement.value = ast.Call(func=constructor, args=[], keywords=[])
            changed = True
        mapping = constructor.args[2]
        pairs = {
            key.value: index
            for index, key in enumerate(mapping.keys)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        for member in contracts[symbol].get("decorator_members", []):
            index = pairs.get(member)
            if index is None:
                mapping.keys.append(ast.Constant(value=member))
                mapping.values.append(ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[], args=[ast.arg(arg="self"), ast.arg(arg="callback")],
                        kwonlyargs=[], kw_defaults=[], defaults=[],
                    ),
                    body=ast.Name(id="callback", ctx=ast.Load()),
                ))
                changed = True
                continue
            member_value = mapping.values[index]
            if isinstance(member_value, ast.Constant) and member_value.value is None or (
                isinstance(member_value, ast.Lambda)
                and isinstance(member_value.body, ast.Constant)
                and member_value.body.value is None
            ):
                mapping.values[index] = ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[], args=[ast.arg(arg="self"), ast.arg(arg="callback")],
                        kwonlyargs=[], kw_defaults=[], defaults=[],
                    ),
                    body=ast.Name(id="callback", ctx=ast.Load()),
                )
                changed = True
        for member_value in mapping.values:
            if not isinstance(member_value, ast.Lambda) or member_value.args.vararg:
                continue
            positional = [*member_value.args.posonlyargs, *member_value.args.args]
            if positional and positional[0].arg in receiver_names:
                continue
            target = (
                member_value.args.posonlyargs
                if member_value.args.posonlyargs else member_value.args.args
            )
            target.insert(0, ast.arg(arg="self"))
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_extension_provider_order(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Place consumer imports after the provider object they import back from."""
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    for decision in (seam_plan or {}).get("decisions", {}).values():
        if decision.get("kind") != "extension_provider" or decision.get(
            "provider"
        ) != path:
            continue
        symbol = decision["symbol"]
        provider = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        if provider is None:
            continue
        provider_index = tree.body.index(provider)
        consumers = decision.get("consumers", [])
        lazy_consumers = decision.get("lazy_consumers", [])
        for statement in list(tree.body):
            modules = []
            local_names: set[str] = set()
            if isinstance(statement, ast.ImportFrom):
                base = _resolve_module(statement.module, statement.level, path)
                modules = [base, *(
                    f"{base}.{alias.name}".lstrip(".") for alias in statement.names
                )]
                local_names = {alias.asname or alias.name for alias in statement.names}
            elif isinstance(statement, ast.Import):
                modules = [alias.name for alias in statement.names]
                local_names = {
                    alias.asname or alias.name.split(".")[0]
                    for alias in statement.names
                }
            consumer = next((
                consumer for consumer in lazy_consumers
                if any(module in _module_names(consumer) for module in modules)
            ), None)
            if consumer is None or not local_names:
                continue
            users = [
                function for function in tree.body
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in local_names for node in ast.walk(function)
                )
            ]
            outside_use = any(
                isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id in local_names
                for top in tree.body
                if top is not statement and top not in users
                for node in ast.walk(top)
            )
            if not users or outside_use:
                continue
            tree.body.remove(statement)
            for function in users:
                function.body.insert(0, deepcopy(statement))
            changed = True
        provider_index = tree.body.index(provider)
        early = []
        for statement in tree.body[:provider_index]:
            modules = []
            if isinstance(statement, ast.ImportFrom):
                base = _resolve_module(statement.module, statement.level, path)
                modules = [base, *(
                    f"{base}.{alias.name}".lstrip(".") for alias in statement.names
                )]
            elif isinstance(statement, ast.Import):
                modules = [alias.name for alias in statement.names]
            if any(
                module in _module_names(consumer)
                for consumer in consumers for module in modules
            ):
                early.append(statement)
        if not early:
            continue
        for statement in early:
            tree.body.remove(statement)
        provider_index = tree.body.index(provider)
        tree.body[provider_index + 1:provider_index + 1] = early
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_cli_factory(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Remove Flask-only command registration when the app-owned facade captures it."""
    decision = (seam_plan or {}).get("decisions", {}).get("standalone_cli", {})
    registrars = decision.get("registrars", [])
    if path not in decision.get("factory_files", []) or not registrars:
        return content
    tree = _parsed(content)
    if tree is None:
        return content
    required_initializers = {
        (initializer["provider"], initializer["symbol"])
        for factory in (seam_plan or {}).get("decisions", {}).values()
        if factory.get("kind") == "application_factory"
        and factory.get("factory") == path
        for initializer in factory.get("initializers", [])
    }
    registrar_names = set()
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        module = _resolve_module(statement.module, statement.level, path)
        for registrar in registrars:
            if (registrar["module"], registrar["function"]) in required_initializers:
                continue
            if module not in _module_names(registrar["module"]):
                continue
            registrar_names.update(
                alias.asname or alias.name
                for alias in statement.names
                if alias.name == registrar["function"]
            )
    changed = False
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        kept = []
        for statement in function.body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id in registrar_names
            ):
                changed = True
                continue
            kept.append(statement)
        function.body = kept
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_extension_provider_facade(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Complete mapping/class facades with the frozen SQLAlchemy object surface."""
    tree = _parsed(content)
    if tree is None:
        return content

    def imported_name(module: str, name: str, asname: str | None = None) -> str:
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == module:
                for alias in statement.names:
                    if alias.name == name:
                        return alias.asname or alias.name
                statement.names.append(ast.alias(name=name, asname=asname))
                return asname or name
        insert_at = 1 if (
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ) else 0
        while (
            insert_at < len(tree.body)
            and isinstance(tree.body[insert_at], ast.ImportFrom)
            and tree.body[insert_at].module == "__future__"
        ):
            insert_at += 1
        tree.body.insert(insert_at, ast.ImportFrom(
            module=module, names=[ast.alias(name=name, asname=asname)], level=0,
        ))
        return asname or name

    changed = False
    for decision in (seam_plan or {}).get("decisions", {}).values():
        if decision.get("kind") != "extension_provider" or decision.get(
            "provider"
        ) != path:
            continue
        symbol = decision["symbol"]
        assignment = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        if assignment is None:
            continue
        required = set(decision.get("members", []))
        fallback_model = next((
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1]
            in {"declarative_base", "DeclarativeBase"}
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        ), "")
        fallback_model = fallback_model or next((
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
            and any(
                ast.unparse(base).split(".")[-1] == "DeclarativeBase"
                for base in node.bases
            )
        ), "")
        database_config = decision.get("database_config", {})
        if database_config:
            config_name = imported_name(
                database_config["module"], database_config["symbol"],
            )
            uri = ast.Attribute(
                value=ast.Subscript(
                    value=ast.Name(id=config_name, ctx=ast.Load()),
                    slice=ast.Constant(database_config["default_key"]),
                    ctx=ast.Load(),
                ),
                attr="SQLALCHEMY_DATABASE_URI", ctx=ast.Load(),
            )
            engine_factory = "create_engine"
            helper = None
            if database_config.get("sqlite"):
                engine_factory = "_portage_create_engine"
                helper = next((
                    node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == engine_factory
                ), None)
                if helper is None:
                    raw_create_engine = imported_name("sqlalchemy", "create_engine")
                    static_pool = imported_name("sqlalchemy.pool", "StaticPool")
                    helper = ast.parse(
                        f"def {engine_factory}(uri):\n"
                        "    value = str(uri)\n"
                        "    kwargs = {}\n"
                        "    if value.startswith('sqlite'):\n"
                        "        kwargs['connect_args'] = {'check_same_thread': False}\n"
                        "        if value in {'sqlite://', 'sqlite:///:memory:'}:\n"
                        f"            kwargs['poolclass'] = {static_pool}\n"
                        f"    return {raw_create_engine}(uri, **kwargs)\n"
                    ).body[0]
                    first_definition = next((
                        index for index, node in enumerate(tree.body)
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    ), len(tree.body))
                    tree.body.insert(first_definition, helper)
                    changed = True
            for statement in tree.body:
                if not (
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement.value, ast.Call)
                    and statement.value.args
                    and "engine" in ast.unparse(statement.value.func).lower()
                ):
                    continue
                statement.value.args[0] = deepcopy(uri)
                changed = True
            for call in (
                node
                for statement in tree.body if statement is not helper
                for node in ast.walk(statement)
                if isinstance(node, ast.Call)
                and ast.unparse(node.func).split(".")[-1] == "create_engine"
                and node.args
            ):
                call.func = ast.Name(id=engine_factory, ctx=ast.Load())
                call.args[0] = deepcopy(uri)
                call.keywords = []
                changed = True
        if decision.get("query_models"):
            base = next((
                node for node in tree.body if isinstance(node, ast.ClassDef)
                and any(
                    isinstance(parent, ast.Name) and parent.id == "DeclarativeBase"
                    or isinstance(parent, ast.Attribute)
                    and parent.attr == "DeclarativeBase"
                    for parent in node.bases
                )
            ), None)
            if base is not None and not any(
                isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == "query"
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
                for statement in base.body
            ):
                descriptor_name = "_PortageModelQuery"
                descriptor = ast.parse(
                    f"class {descriptor_name}:\n"
                    "    def __get__(self, instance, model):\n"
                    f"        return {symbol}.session.query(model)\n"
                ).body[0]
                tree.body.insert(tree.body.index(base), descriptor)
                base.body.append(ast.Assign(
                    targets=[ast.Name(id="query", ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Name(id=descriptor_name, ctx=ast.Load()),
                        args=[], keywords=[],
                    ),
                ))
                changed = True
        dynamic_type = (
            assignment.value
            if isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id == "type"
            and len(assignment.value.args) == 3
            and isinstance(assignment.value.args[2], ast.Dict)
            else assignment.value.func
            if isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Call)
            and isinstance(assignment.value.func.func, ast.Name)
            and assignment.value.func.func.id == "type"
            and len(assignment.value.func.args) == 3
            and isinstance(assignment.value.func.args[2], ast.Dict)
            else None
        )
        if dynamic_type is not None:
            assignment.value = deepcopy(dynamic_type.args[2])
            changed = True
        namespace_names = {"SimpleNamespace"}
        namespace_names.update(
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "types"
            for alias in statement.names if alias.name == "SimpleNamespace"
        )
        if (
            isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id in namespace_names
            and not assignment.value.args
            and all(keyword.arg for keyword in assignment.value.keywords)
        ):
            assignment.value = ast.Dict(
                keys=[ast.Constant(keyword.arg) for keyword in assignment.value.keywords],
                values=[keyword.value for keyword in assignment.value.keywords],
            )
            changed = True
        if (
            isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
        ):
            facade = next((
                node for node in tree.body if isinstance(node, ast.ClassDef)
                and node.name == assignment.value.func.id
            ), None)
            if facade is None:
                continue
            original_body_size = len(facade.body)
            values = {
                target.id: statement.value
                for statement in facade.body
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            initializer = next((
                node for node in facade.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__init__"
            ), None)
            if initializer is not None:
                parameters = [
                    *initializer.args.posonlyargs, *initializer.args.args,
                ][1:]
                arguments = {
                    parameter.arg: argument
                    for parameter, argument in zip(
                        parameters, assignment.value.args, strict=False,
                    )
                }
                arguments.update({
                    keyword.arg: keyword.value for keyword in assignment.value.keywords
                    if keyword.arg
                })
                for statement in ast.walk(initializer):
                    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and isinstance(statement.value, ast.Name)
                            and statement.value.id in arguments
                        ):
                            values[target.attr] = arguments[statement.value.id]

            def add_class_member(
                name: str, value: ast.expr, *, owner=facade, members=values,
            ) -> None:
                owner.body.append(ast.Assign(
                    targets=[ast.Name(id=name, ctx=ast.Store())], value=value,
                ))
                members[name] = value

            if "Model" in required and "Model" not in values:
                model = values.get("Base") or (
                    ast.Name(id=fallback_model, ctx=ast.Load()) if fallback_model else None
                )
                if model is not None:
                    add_class_member("Model", deepcopy(model))
            for member in required & {"case", "event"}:
                if member not in values:
                    add_class_member(
                        member,
                        ast.Name(id=imported_name("sqlalchemy", member), ctx=ast.Load()),
                    )
            if (
                "metadata" in required and "metadata" not in values
                and values.get("Model")
            ):
                add_class_member("metadata", ast.Attribute(
                    value=deepcopy(values["Model"]), attr="metadata", ctx=ast.Load(),
                ))
            if "session" in required and values.get("SessionLocal"):
                existing_session = values.get("session")
                raw_session = (
                    existing_session is None
                    or isinstance(existing_session, ast.Call)
                    and ast.dump(existing_session.func) == ast.dump(values["SessionLocal"])
                )
                if raw_session:
                    session = ast.Call(
                        func=ast.Name(
                            id=imported_name("sqlalchemy.orm", "scoped_session"),
                            ctx=ast.Load(),
                        ),
                        args=[deepcopy(values["SessionLocal"])], keywords=[],
                    )
                    if existing_session is None:
                        add_class_member("session", session)
                    else:
                        for statement in facade.body:
                            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and any(
                                isinstance(target, ast.Name) and target.id == "session"
                                for target in (
                                    statement.targets if isinstance(statement, ast.Assign)
                                    else [statement.target]
                                )
                            ):
                                statement.value = session
                                values["session"] = session
                                break
            defined = {
                node.name for node in facade.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if database_config and "init_app" in required:
                method = ast.parse(
                    "def init_app(self, app):\n"
                    "    uri = app.state.config.get('SQLALCHEMY_DATABASE_URI')\n"
                    "    if uri and str(self.engine.url) != uri:\n"
                    "        self.session.remove()\n"
                    f"        self.engine = {engine_factory}(uri)\n"
                    "        self.session.configure(bind=self.engine)\n"
                ).body[0]
                facade.body = [
                    node for node in facade.body
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or node.name != "init_app"
                ]
                facade.body.append(method)
                defined.add("init_app")
                changed = True
            for member in sorted(required & {"create_all", "drop_all"} - defined):
                if not (values.get("metadata") and values.get("engine")):
                    continue
                method = ast.parse(
                    f"def {member}(self, *args, **kwargs):\n"
                    "    kwargs.pop('bind_key', None)\n"
                    "    kwargs.setdefault('bind', self.engine)\n"
                    f"    return self.metadata.{member}(*args, **kwargs)\n"
                ).body[0]
                facade.body.append(method)
            changed = changed or len(facade.body) != original_body_size
            continue
        if not isinstance(assignment.value, ast.Dict):
            continue
        pairs = [
            (key.value, value)
            for key, value in zip(
                assignment.value.keys, assignment.value.values, strict=True,
            )
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
            and key.value.isidentifier()
        ]
        if len(pairs) != len(assignment.value.keys):
            continue
        values = dict(pairs)
        if "Model" in required and "Model" not in values:
            model = values.get("Base") or next((
                ast.Name(id=node.name, ctx=ast.Load())
                for node in tree.body if isinstance(node, ast.ClassDef)
                and any(
                    isinstance(base, ast.Name) and base.id == "DeclarativeBase"
                    or isinstance(base, ast.Attribute)
                    and base.attr == "DeclarativeBase"
                    for base in node.bases
                )
            ), ast.Name(id=fallback_model, ctx=ast.Load()) if fallback_model else None)
            if model is not None:
                pairs.append(("Model", model))
                values["Model"] = model
        for member in required & {"case", "event"}:
            if member not in values or (
                isinstance(values[member], ast.Constant)
                and values[member].value is None
            ):
                local = imported_name(
                    "sqlalchemy", member, f"_portage_sqlalchemy_{member}",
                )
                replacement = ast.Name(id=local, ctx=ast.Load())
                if member in values:
                    pairs = [
                        (key, replacement if key == member else value)
                        for key, value in pairs
                    ]
                else:
                    pairs.append((member, replacement))
                values[member] = replacement
        if "metadata" in required and "metadata" not in values and values.get("Model"):
            metadata = ast.Attribute(
                value=deepcopy(values["Model"]), attr="metadata", ctx=ast.Load(),
            )
            pairs.append(("metadata", metadata))
            values["metadata"] = metadata
        if "session" in required and values.get("SessionLocal"):
            existing_session = values.get("session")
            raw_session = (
                existing_session is None
                or isinstance(existing_session, ast.Call)
                and ast.dump(existing_session.func) == ast.dump(values["SessionLocal"])
            )
            if raw_session:
                session = ast.Call(
                    func=ast.Name(
                        id=imported_name("sqlalchemy.orm", "scoped_session"),
                        ctx=ast.Load(),
                    ),
                    args=[deepcopy(values["SessionLocal"])], keywords=[],
                )
                if existing_session is None:
                    pairs.append(("session", session))
                else:
                    pairs = [
                        (key, session if key == "session" else value)
                        for key, value in pairs
                    ]
                values["session"] = session
        insertion = tree.body.index(assignment)
        for member in sorted(required & {"create_all", "drop_all"}):
            if not (values.get("metadata") and values.get("engine")):
                continue
            helper_name = f"_portage_{symbol}_{member}"
            if not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == helper_name for node in tree.body
            ):
                helper = ast.parse(
                    f"def {helper_name}(*args, **kwargs):\n"
                    "    kwargs.pop('bind_key', None)\n"
                    f"    kwargs.setdefault('bind', {symbol}.engine)\n"
                    f"    return {symbol}.metadata.{member}(*args, **kwargs)\n"
                ).body[0]
                tree.body.insert(insertion, helper)
                insertion += 1
            value = ast.Name(id=helper_name, ctx=ast.Load())
            if member in values:
                pairs = [
                    (key, value if key == member else existing)
                    for key, existing in pairs
                ]
            else:
                pairs.append((member, value))
            values[member] = value
        namespace = imported_name("types", "SimpleNamespace")
        assignment.value = ast.Call(
            func=ast.Name(id=namespace, ctx=ast.Load()), args=[],
            keywords=[
                ast.keyword(arg=key, value=value) for key, value in pairs
            ],
        )
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_implicit_sqlalchemy_tables(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Materialize table names that Flask-SQLAlchemy supplied implicitly."""
    expected = {
        class_name: table_name
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "extension_provider"
        for class_name, table_name in decision.get("implicit_tables", {}).get(
            path, {},
        ).items()
    }
    tree = _parsed(content)
    if tree is None or not expected:
        return content
    changed = False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in expected:
            continue
        if any(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id in {"__table__", "__tablename__"}
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
            for statement in node.body
        ):
            continue
        insert_at = int(bool(
            node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ))
        node.body.insert(insert_at, ast.Assign(
            targets=[ast.Name(id="__tablename__", ctx=ast.Store())],
            value=ast.Constant(value=expected[node.name]),
        ))
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _incomplete_object_config(value: ast.AST) -> ast.AST | None:
    """Return the object copied by vars()/__dict__, which drops inherited settings."""
    if (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        and value.func.id == "vars" and len(value.args) == 1
    ):
        return value.args[0]
    if isinstance(value, ast.Attribute) and value.attr == "__dict__":
        return value.value
    if (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        and value.func.id == "dict" and len(value.args) == 1
    ):
        return _incomplete_object_config(value.args[0])
    return None


def _uppercase_object_config(value: ast.AST) -> ast.DictComp:
    name = "_portage_config_key"
    return ast.DictComp(
        key=ast.Name(id=name, ctx=ast.Load()),
        value=ast.Call(
            func=ast.Name(id="getattr", ctx=ast.Load()),
            args=[deepcopy(value), ast.Name(id=name, ctx=ast.Load())],
            keywords=[],
        ),
        generators=[ast.comprehension(
            target=ast.Name(id=name, ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(id="dir", ctx=ast.Load()),
                args=[deepcopy(value)], keywords=[],
            ),
            ifs=[ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id=name, ctx=ast.Load()),
                    attr="isupper", ctx=ast.Load(),
                ),
                args=[], keywords=[],
            )],
            is_async=0,
        )],
    )


def _copies_object_config(value: ast.AST, source: ast.AST) -> bool:
    wanted = ast.dump(source, include_attributes=False)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "dir"
        and len(node.args) == 1
        and ast.dump(node.args[0], include_attributes=False) == wanted
        for node in ast.walk(value)
    )


def _realize_factory_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Materialize frozen factory wiring that has one mechanical realization."""
    decisions = (seam_plan or {}).get("decisions", {}).values()
    decision = next((
        item for item in decisions
        if item.get("kind") == "application_factory" and item.get("factory") == path
    ), None)
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    factory = next((
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "create_app"
    ), None)
    test_surface = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "planned_test_surface"
        and path in item.get("files", []) and item.get("provider") != path
    ), None)
    classes = (test_surface or {}).get("classes", [])
    facade_names: set[str] = set()
    if len(classes) == 1:
        provider = test_surface["provider"]
        class_name = classes[0]["name"]
        facade_names = {
            alias.asname or alias.name
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(provider)
            for alias in statement.names if alias.name == class_name
        }
        if not facade_names:
            tree.body.insert(0, ast.ImportFrom(
                module=provider.removesuffix(".py").replace("/", "."),
                names=[ast.alias(name=class_name)], level=0,
            ))
            facade_names = {class_name}
            changed = True
        facade_name = next(iter(facade_names))
        returned = {
            node.value.id for node in ast.walk(factory)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        } if factory else set()
        assignments = [
            statement
            for scope in [tree, *([factory] if factory else [])]
            for statement in scope.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1] == "FastAPI"
            and any(
                isinstance(target, ast.Name)
                and (scope is tree or target.id in returned)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ]
        for statement in assignments:
            statement.value.func = ast.Name(id=facade_name, ctx=ast.Load())
            changed = True
    if decision and factory:
        for contract in decision.get("local_imports", []):
            moved = []
            for statement in list(tree.body):
                if not (
                    isinstance(statement, ast.ImportFrom)
                    and _resolve_module(statement.module, statement.level, path)
                    == contract["module"]
                ):
                    continue
                selected = [
                    alias for alias in statement.names
                    if alias.name == contract["symbol"]
                ]
                if not selected:
                    continue
                moved.extend(selected)
                statement.names = [
                    alias for alias in statement.names if alias not in selected
                ]
                if not statement.names:
                    tree.body.remove(statement)
            if not moved:
                continue
            local_names = {alias.asname or alias.name for alias in moved}
            insert_at = next((
                index for index, statement in enumerate(factory.body)
                if any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in local_names
                    for node in ast.walk(statement)
                )
            ), next((
                index for index, statement in enumerate(factory.body)
                if isinstance(statement, ast.Return)
            ), len(factory.body)))
            factory.body.insert(insert_at, ast.ImportFrom(
                module=contract.get("source_module", contract["module"]),
                names=moved, level=contract.get("level", 0),
            ))
            changed = True
    if decision and decision.get("config_from_objects"):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and ast.unparse(node.func.value).endswith(".config")
                and node.args
                and (source := _incomplete_object_config(node.args[0])) is not None
            ):
                node.args[0] = _uppercase_object_config(source)
                changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(ast.unparse(target).endswith(".config") for target in targets):
                    continue
                source = _incomplete_object_config(node.value)
                if source is not None:
                    node.value = _uppercase_object_config(source)
                    changed = True
        sources = []
        for expression in decision["config_from_objects"]:
            try:
                sources.append(ast.parse(expression, mode="eval").body)
            except SyntaxError:
                continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(
                ast.unparse(target).endswith(".state.config") for target in targets
            ):
                continue
            missing = [
                source for source in sources
                if not _copies_object_config(node.value, source)
            ]
            defaults = None
            for source in missing:
                mapping = _uppercase_object_config(source)
                defaults = mapping if defaults is None else ast.BinOp(
                    left=defaults, op=ast.BitOr(), right=mapping,
                )
            if defaults is not None:
                node.value = ast.BinOp(
                    left=defaults, op=ast.BitOr(), right=node.value,
                )
                changed = True
    if decision and factory and decision.get("initializers"):
        returned_apps = {
            node.value.id for node in ast.walk(factory)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        returned_apps.update(
            node.value.args[0].id for node in ast.walk(factory)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call) and node.value.args
            and isinstance(node.value.args[0], ast.Name)
        )
        configured_apps = {
            node.value.value.id
            for node in ast.walk(factory)
            if isinstance(node, ast.Attribute) and node.attr == "config"
            and isinstance(node.value, ast.Attribute) and node.value.attr == "state"
            and isinstance(node.value.value, ast.Name)
        }
        constructed_apps = {
            target.id
            for statement in ast.walk(factory)
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1]
            in {"FastAPI", *facade_names}
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        wired_apps = {
            node.func.value.id
            for node in ast.walk(factory)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in {
                "add_exception_handler", "add_middleware", "api_route", "delete",
                "get", "head", "include_router", "mount", "options", "patch",
                "post", "put",
            }
        }
        source_apps = {
            argument.id
            for initializer in decision["initializers"]
            for argument in ast.parse(
                initializer["original_call"], mode="eval",
            ).body.args
            if isinstance(argument, ast.Name)
            and any(
                isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                and node.id == argument.id
                for node in ast.walk(factory)
            )
        }
        candidate_sets = (
            returned_apps, configured_apps, constructed_apps, wired_apps, source_apps,
        )
        nonempty_candidates = [item for item in candidate_sets if item]
        overlap = (
            set.intersection(*nonempty_candidates) if nonempty_candidates else set()
        )
        scores = {
            name: sum(name in candidates for candidates in candidate_sets)
            for candidates in candidate_sets for name in candidates
        }
        best_score = max(scores.values(), default=0)
        best = {name for name, score in scores.items() if score == best_score}
        app_names = next(
            (item for item in candidate_sets if len(item) == 1),
            overlap if len(overlap) == 1 else best,
        )
        pending: list[ast.Call] = []
        for initializer in decision["initializers"]:
            wanted = _module_names(initializer["provider"])
            tails = {name.split(".")[-1] for name in wanted}
            refs: set[str] = set()
            for statement in [*tree.body, *factory.body]:
                if isinstance(statement, ast.ImportFrom):
                    module = _resolve_module(statement.module, statement.level, path)
                    if module in wanted or module.split(".")[-1] in tails:
                        refs.update(
                            alias.asname or alias.name
                            for alias in statement.names
                            if alias.name == initializer["symbol"]
                        )
                    for alias in statement.names:
                        if f"{module}.{alias.name}".lstrip(".") in wanted:
                            refs.add(
                                f"{alias.asname or alias.name}."
                                f"{initializer['symbol']}"
                            )
                elif isinstance(statement, ast.Import):
                    refs.update(
                        f"{alias.asname or alias.name}.{initializer['symbol']}"
                        for alias in statement.names
                        if alias.name in wanted or alias.name.split(".")[-1] in tails
                    )
            if any(
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in refs
                for node in ast.walk(factory)
            ):
                continue
            call = ast.parse(initializer["original_call"], mode="eval").body
            root = next(
                node.id for node in ast.walk(call.func) if isinstance(node, ast.Name)
            )
            bound = any(
                isinstance(statement, ast.ImportFrom)
                and any(
                    (alias.asname or alias.name) == root
                    for alias in statement.names
                )
                for statement in [*tree.body, *factory.body]
            )
            if not bound:
                local = next((
                    item for item in decision.get("local_imports", [])
                    if item["symbol"] == root
                ), None)
                if local:
                    import_at = int(bool(
                        factory.body and isinstance(factory.body[0], ast.Expr)
                        and isinstance(factory.body[0].value, ast.Constant)
                        and isinstance(factory.body[0].value.value, str)
                    ))
                    factory.body.insert(import_at, ast.ImportFrom(
                        module=local.get("source_module", local["module"]),
                        names=[ast.alias(name=root)], level=local.get("level", 0),
                    ))
                else:
                    factory.body.insert(0, ast.ImportFrom(
                        module=initializer["provider"].removesuffix(".py").replace(
                            "/", "."
                        ),
                        names=[ast.alias(name=initializer["symbol"])], level=0,
                    ))
                    call.func = ast.Name(id=initializer["symbol"], ctx=ast.Load())
            pending.append(call)

        if pending and len(app_names) == 1:
            app_name = next(iter(app_names))
            for call in pending:
                call.args = [ast.Name(id=app_name, ctx=ast.Load())]
            config_indices = [
                index for index, statement in enumerate(factory.body)
                if any(
                    ast.unparse(node).endswith(".state.config")
                    or ast.unparse(node).endswith(".state.config.update")
                    for node in ast.walk(statement)
                    if isinstance(node, (ast.Attribute, ast.Name))
                )
            ]
            binding_indices = [
                index for index, statement in enumerate(factory.body)
                if any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                    and node.id == app_name
                    for node in ast.walk(statement)
                )
            ]
            cursor = max([*config_indices, *binding_indices], default=-1) + 1
            for call in pending:
                factory.body.insert(cursor, ast.Expr(value=call))
                cursor += 1
            changed = True
    ambient = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "ambient_context_runtime"
        and path in item.get("factory_files", [])
    ), None)
    if ambient:
        runtime_modules = {
            PurePosixPath(provider).stem
            for provider in ambient.get("runtime_providers", [])
        }
        runtime_class_exports = {
            name for names in ambient.get("runtime_classes", {}).values()
            for name in names
        }
        context_names = {
            alias.asname or alias.name
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and (statement.module or "").split(".")[-1] in runtime_modules
            for alias in statement.names if alias.name in runtime_class_exports
        }
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            middleware = [
                (index, statement, statement.value.args[0].id)
                for index, statement in enumerate(function.body)
                if isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and statement.value.func.attr == "add_middleware"
                and statement.value.args
                and isinstance(statement.value.args[0], ast.Name)
            ]
            sessions = [item for item in middleware if item[2] == "SessionMiddleware"]
            contexts = [item for item in middleware if item[2] in context_names]
            if len(sessions) == len(contexts) == 1 and sessions[0][0] < contexts[0][0]:
                session_statement = sessions[0][1]
                function.body.remove(session_statement)
                context_index = function.body.index(contexts[0][1])
                function.body.insert(context_index + 1, session_statement)
                changed = True
            if len(sessions) == 1:
                session_statement = sessions[0][1]
                inline_middleware = [
                    statement for statement in function.body
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "middleware"
                        for decorator in statement.decorator_list
                    )
                ]
                if inline_middleware and function.body.index(session_statement) < max(
                    function.body.index(statement) for statement in inline_middleware
                ):
                    function.body.remove(session_statement)
                    last_middleware = max(
                        inline_middleware, key=lambda statement: function.body.index(statement)
                    )
                    function.body.insert(
                        function.body.index(last_middleware) + 1, session_statement,
                    )
                    changed = True
            returned_apps = {
                node.value.id for node in ast.walk(function)
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
            }
            installed = any(
                isinstance(call, ast.Call) and call.args
                and (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "add_middleware"
                    or ast.unparse(call.func).split(".")[-1] == "Middleware"
                )
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id in context_names
                for call in ast.walk(function)
            )
            if len(returned_apps) == len(context_names) == 1 and not installed:
                app_name = next(iter(returned_apps))
                context_name = next(iter(context_names))
                install = ast.Expr(value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id=app_name, ctx=ast.Load()),
                        attr="add_middleware", ctx=ast.Load(),
                    ),
                    args=[ast.Name(id=context_name, ctx=ast.Load())], keywords=[],
                ))
                insert_at = next((
                    index for index, statement in enumerate(function.body)
                    if isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr == "add_middleware"
                    and statement.value.args
                    and isinstance(statement.value.args[0], ast.Name)
                    and statement.value.args[0].id == "SessionMiddleware"
                ), next((
                    index for index, statement in enumerate(function.body)
                    if isinstance(statement, ast.Return)
                ), len(function.body)))
                function.body.insert(insert_at, install)
                changed = True
        for keyword in (
            keyword
            for call in ast.walk(tree) if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "middleware"
            and isinstance(keyword.value, (ast.List, ast.Tuple))
        ):
            specs = keyword.value.elts
            sessions = [
                index for index, item in enumerate(specs)
                if isinstance(item, ast.Call) and item.args
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id == "SessionMiddleware"
            ]
            contexts = [
                index for index, item in enumerate(specs)
                if isinstance(item, ast.Call) and item.args
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id in context_names
            ]
            if len(sessions) == len(contexts) == 1 and sessions[0] > contexts[0]:
                session = specs.pop(sessions[0])
                specs.insert(contexts[0], session)
                changed = True
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            invalid = [
                keyword for keyword in call.keywords
                if keyword.arg == "lifespan"
                and any(
                    isinstance(node, ast.Name) and node.id in context_names
                    for node in ast.walk(keyword.value)
                )
            ]
            if invalid:
                call.keywords = [
                    keyword for keyword in call.keywords if keyword not in invalid
                ]
                changed = True

    global_hooks = [
        (item["path"], hook["function"])
        for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "request_hooks" and path in item.get("files", [])
        for hook in item.get("hooks", [])
        if hook.get("scope") == "before_app_request"
    ]
    returned_apps = {
        node.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }
    constructors = [
        statement.value
        for statement in ast.walk(tree)
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and any(
            isinstance(target, ast.Name) and target.id in returned_apps
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        )
    ]
    if len(constructors) == 1:
        depends_name = next((
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
            for alias in statement.names if alias.name == "Depends"
        ), None)
        if global_hooks and depends_name is None:
            fastapi_import = next((
                statement for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and statement.module == "fastapi" and statement.level == 0
            ), None)
            if fastapi_import is None:
                fastapi_import = ast.ImportFrom(
                    module="fastapi", names=[], level=0,
                )
                insert_at = 1 if (
                    tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Constant)
                    and isinstance(tree.body[0].value.value, str)
                ) else 0
                while (
                    insert_at < len(tree.body)
                    and isinstance(tree.body[insert_at], ast.ImportFrom)
                    and tree.body[insert_at].module == "__future__"
                ):
                    insert_at += 1
                tree.body.insert(insert_at, fastapi_import)
            fastapi_import.names.append(ast.alias(name="Depends"))
            depends_name = "Depends"
            changed = True

        dependencies = next(
            (keyword for keyword in constructors[0].keywords
             if keyword.arg == "dependencies"),
            None,
        )
        for provider, name in sorted(global_hooks):
            provider_import = next((
                statement for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path)
                in _module_names(provider)
            ), None)
            local_name = next((
                alias.asname or alias.name
                for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path)
                in _module_names(provider)
                for alias in statement.names if alias.name == name
            ), None)
            if local_name is None:
                if provider_import is None:
                    module = provider.removesuffix(".py").replace("/", ".")
                    provider_import = ast.ImportFrom(
                        module=module, names=[], level=0,
                    )
                    insert_at = 1 if (
                        tree.body and isinstance(tree.body[0], ast.Expr)
                        and isinstance(tree.body[0].value, ast.Constant)
                        and isinstance(tree.body[0].value.value, str)
                    ) else 0
                    while insert_at < len(tree.body) and isinstance(
                        tree.body[insert_at], (ast.Import, ast.ImportFrom)
                    ):
                        insert_at += 1
                    tree.body.insert(insert_at, provider_import)
                provider_import.names.append(ast.alias(name=name))
                local_name = name
                changed = True
            call = ast.Call(
                func=ast.Name(id=depends_name or "Depends", ctx=ast.Load()),
                args=[ast.Name(id=local_name, ctx=ast.Load())], keywords=[],
            )
            values = dependencies.value.elts if dependencies and isinstance(
                dependencies.value, (ast.List, ast.Tuple)
            ) else []
            if any(ast.dump(value) == ast.dump(call) for value in values):
                continue
            if dependencies is None:
                dependencies = ast.keyword(
                    arg="dependencies", value=ast.List(elts=[], ctx=ast.Load()),
                )
                constructors[0].keywords.append(dependencies)
                values = dependencies.value.elts
            if isinstance(dependencies.value, (ast.List, ast.Tuple)):
                dependencies.value.elts.append(deepcopy(call))
                changed = True

    aliases = decision.get("endpoint_aliases", []) if decision else []
    if aliases:
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            kept = [
                statement for statement in function.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr in {"append", "extend", "insert"}
                    and ast.unparse(statement.value.func.value).endswith(
                        ".router.routes"
                    )
                )
            ]
            changed |= len(kept) != len(function.body)
            function.body = kept
    existing = {
        (
            node.args[0].value,
            next((
                keyword.value.value for keyword in node.keywords
                if keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ), ""),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {
            "add_api_route", "add_route", "api_route", "get", "post", "put",
            "patch", "delete", "options", "head",
        }
        and node.args and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    missing = [
        alias for alias in aliases
        if (alias["path"], alias["name"]) not in existing
    ]
    if missing:
        returns = [
            (function, index, statement.value.id)
            for function in tree.body
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for index, statement in enumerate(function.body)
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
        ]
        if len(returns) == 1:
            function, index, app_name = returns[0]
            function.body[index:index] = [
                ast.parse(
                    f"{app_name}.add_api_route({alias['path']!r}, lambda: None, "
                    f"name={alias['name']!r}, include_in_schema=False)"
                ).body[0]
                for alias in missing
            ]
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_resource_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Remove duplicate cleanup wiring already owned by the frozen app facade."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "resource_lifecycle" and item.get("module") == path
    ]
    tree = _parsed(content)
    if not decisions or tree is None:
        return content
    changed = False
    for decision in decisions:
        initializer_name = decision.get("initializer")
        cleanup_names = set(decision.get("cleanup_functions", []))
        initializer = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == initializer_name
        ), None)
        if initializer is None or not initializer.args.args:
            continue
        app_name = initializer.args.args[0].arg

        def duplicate_cleanup(
            statement: ast.stmt,
            prefix: str = f"{app_name}.state.",
            cleanups: frozenset[str] = frozenset(cleanup_names),
        ) -> bool:
            return bool(
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and statement.value.func.attr == "append"
                and ast.unparse(statement.value.func.value).startswith(prefix)
                and statement.value.args
                and isinstance(statement.value.args[0], ast.Name)
                and statement.value.args[0].id in cleanups
            )

        kept = [statement for statement in initializer.body if not duplicate_cleanup(statement)]
        changed |= len(kept) != len(initializer.body)
        initializer.body = kept
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_resource_consumers(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Keep yield dependencies in DI and ordinary callers on the direct helper."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "resource_lifecycle"
        and item.get("module") != path and path in item.get("files", [])
        and item.get("symbol") and item.get("dependency")
    ]
    tree = _parsed(content)
    if not decisions or tree is None:
        return content

    bound = {
        name
        for statement in tree.body
        for name in (
            [statement.name]
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [alias.asname or alias.name.split(".")[0] for alias in statement.names]
            if isinstance(statement, (ast.Import, ast.ImportFrom))
            else [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement, ast.Assign)
            else []
        )
    }
    imports: dict[str, tuple[ast.ImportFrom | None, str | None, str]] = {}
    for decision in decisions:
        owner = decision["module"]
        dependency = decision["dependency"]
        symbol = decision["symbol"]
        wanted = _module_names(owner)
        tails = {name.split(".")[-1] for name in wanted}
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom):
                module = _resolve_module(statement.module, statement.level, path)
                if module in wanted or module.split(".")[-1] in tails:
                    direct = next((
                        alias.asname or alias.name
                        for alias in statement.names if alias.name == symbol
                    ), None)
                    for alias in statement.names:
                        if alias.name == dependency:
                            imports[alias.asname or alias.name] = (
                                statement, direct, symbol,
                            )
                for alias in statement.names:
                    candidate = f"{module}.{alias.name}".lstrip(".")
                    if candidate in wanted:
                        local = alias.asname or alias.name
                        imports[f"{local}.{dependency}"] = (
                            None, f"{local}.{symbol}", symbol,
                        )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name in wanted or alias.name.split(".")[-1] in tails:
                        local = alias.asname or alias.name
                        imports[f"{local}.{dependency}"] = (
                            None, f"{local}.{symbol}", symbol,
                        )
    if not imports:
        return content

    parents = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    ordinary = {
        ref for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (ref := ast.unparse(node.func)) in imports
        and not (
            isinstance(parents.get(node), ast.Call)
            and ast.unparse(parents[node].func).split(".")[-1] == "Depends"
        )
    }
    direct_refs: dict[str, str] = {}
    for ref, (statement, direct, symbol) in imports.items():
        if ref not in ordinary:
            direct_refs[ref] = direct or symbol
            continue
        if direct is None and statement is not None:
            direct = symbol
            if direct in bound:
                stem = f"_portage_direct_{symbol}"
                direct = stem
                suffix = 2
                while direct in bound:
                    direct = f"{stem}_{suffix}"
                    suffix += 1
            statement.names.append(ast.alias(
                name=symbol, asname=None if direct == symbol else direct,
            ))
            bound.add(direct)
        direct_refs[ref] = direct or symbol

    class Realize(ast.NodeTransformer):
        @staticmethod
        def _ref(node: ast.AST) -> str:
            return ast.unparse(node)

        @staticmethod
        def _direct(call: ast.Call, ref: str) -> ast.Call:
            return ast.copy_location(ast.Call(
                func=ast.parse(direct_refs[ref], mode="eval").body,
                args=call.args, keywords=call.keywords,
            ), call)

        def visit_Await(self, node: ast.Await) -> ast.AST:
            if (
                isinstance(node.value, ast.Call)
                and (ref := self._ref(node.value.func)) in ordinary
            ):
                return self.visit(self._direct(node.value, ref))
            return self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> ast.AST:
            if self._ref(node.func).split(".")[-1] == "Depends":
                node.args = [
                    argument.func
                    if isinstance(argument, ast.Call)
                    and self._ref(argument.func) in imports
                    else self.visit(argument)
                    for argument in node.args
                ]
                node.keywords = [self.visit(keyword) for keyword in node.keywords]
                return node
            if (
                self._ref(node.func) == "next" and len(node.args) == 1
                and isinstance(node.args[0], ast.Call)
                and (ref := self._ref(node.args[0].func)) in ordinary
            ):
                return self.visit(self._direct(node.args[0], ref))
            node = self.generic_visit(node)
            ref = self._ref(node.func)
            return self._direct(node, ref) if ref in ordinary else node

    tree = Realize().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"
