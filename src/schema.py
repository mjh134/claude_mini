# -*- coding: utf-8 -*-
"""按工具的 input_schema 校验模型传入的参数,由 claude 的 _excute_tool 在调 handler 前调用。
用法:errors, warnings = check_schema(schema, arguments);errors 非空即拦下,warnings 只拼进结果不拦。
只认项目 schema 用到的子集(type/properties/required/enum/items);查不到 schema 当空对象=所有键仅警告。"""

TYPE_NAME = {str: "string", int: "integer", float: "number",
             bool: "boolean", list: "array", dict: "object", type(None): "null"}


def check_schema(schema, arguments):
    """返回 (errors, warnings):errors 是要拦下的问题,warnings 是未知键提示。"""
    if not isinstance(arguments, dict):
        return [f"参数必须是对象(object),实际是 {kind_name(arguments)}"], []

    props = schema.get("properties", {})
    errors, warnings = [], []

    for key in schema.get("required", []):
        if key not in arguments:
            errors.append(f"缺少必填参数:{key}")

    for key, value in arguments.items():
        spec = props.get(key)
        if spec is None:
            warnings.append(f"未声明的参数:{key}(已忽略)")
            continue
        problem = check_value(value, spec)
        if problem:
            errors.append(f"参数 {key} 有误:{problem}")

    return errors, warnings


def attach_warnings(result, warnings):
    """把未知键警告拼到结果尾部;无警告时原样返回,保留 handler 的原始返回类型。"""
    if not warnings:
        return result
    return str(result) + "\n⚠ " + "；".join(warnings)


def check_value(value, spec):
    if "enum" in spec and value not in spec["enum"]:
        return f"取值须是 {spec['enum']} 之一,实际是 {value!r}"
    type_name = spec.get("type")
    if type_name is None:
        return None
    return check_type(value, type_name, spec)


def check_type(value, type_name, spec):
    if type_name == "string":
        return expect_type("string", value) if not isinstance(value, str) else None
    if type_name == "integer":
        # bool 是 int 的子类:isinstance(True, int) 为真,不排除的话 True 会蒙过 integer
        if isinstance(value, bool) or not isinstance(value, int):
            return expect_type("integer", value)
    elif type_name == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return expect_type("number", value)
    elif type_name == "boolean":
        # 只认真布尔:"true"/1 放过去,下游 .get("is_background") 会把它当真值静默走错分支
        if not isinstance(value, bool):
            return expect_type("boolean", value)
    elif type_name == "array":
        if not isinstance(value, list):
            return expect_type("array", value)
        item_spec = spec.get("items")
        for i, item in enumerate(value):
            if item_spec:
                problem = check_value(item, item_spec)
                if problem:
                    return f"第 {i} 个元素:{problem}"
    elif type_name == "object":
        if not isinstance(value, dict):
            return expect_type("object", value)
    return None


def expect_type(type_name, value):
    return f"应为 {type_name},实际是 {kind_name(value)}({value!r})"


def kind_name(value):
    return TYPE_NAME.get(type(value), type(value).__name__)
