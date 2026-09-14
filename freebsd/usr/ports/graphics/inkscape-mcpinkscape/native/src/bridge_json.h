// SPDX-License-Identifier: GPL-2.0-or-later
// Small bounded JSON parser for the native bridge protocol.
#ifndef MCPINKSCAPE_BRIDGE_JSON_H
#define MCPINKSCAPE_BRIDGE_JSON_H

#include <cctype>
#include <cmath>
#include <cstdlib>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace mcpinkscape::json {

struct Value {
    enum class Type { null, boolean, number, string, array, object };

    Type type = Type::null;
    bool boolean = false;
    double number = 0;
    std::string string;
    std::vector<Value> array;
    std::map<std::string, Value> object;

    Value const *get(std::string const &key) const
    {
        if (type != Type::object) return nullptr;
        auto const it = object.find(key);
        return it == object.end() ? nullptr : &it->second;
    }
};

class Parser final {
public:
    explicit Parser(std::string const &input)
        : _input(input)
    {}

    std::optional<Value> parse(std::string &error)
    {
        Value value;
        skip_space();
        if (!parse_value(value, error, 0)) return std::nullopt;
        skip_space();
        if (_position != _input.size()) {
            error = "trailing data after JSON value";
            return std::nullopt;
        }
        return value;
    }

private:
    static constexpr std::size_t MAX_DEPTH = 32;

    bool parse_value(Value &output, std::string &error, std::size_t depth)
    {
        if (depth > MAX_DEPTH) {
            error = "JSON nesting exceeds bridge limit";
            return false;
        }
        skip_space();
        if (_position >= _input.size()) {
            error = "unexpected end of JSON";
            return false;
        }
        switch (_input[_position]) {
            case '{': return parse_object(output, error, depth + 1);
            case '[': return parse_array(output, error, depth + 1);
            case '"':
                output.type = Value::Type::string;
                return parse_string(output.string, error);
            case 't': return parse_literal(output, "true", Value::Type::boolean, true, error);
            case 'f': return parse_literal(output, "false", Value::Type::boolean, false, error);
            case 'n': return parse_null(output, error);
            default: return parse_number(output, error);
        }
    }

    bool parse_object(Value &output, std::string &error, std::size_t depth)
    {
        output = Value{};
        output.type = Value::Type::object;
        ++_position;
        skip_space();
        if (consume('}')) return true;
        while (true) {
            std::string key;
            if (!parse_string(key, error)) return false;
            skip_space();
            if (!consume(':')) {
                error = "object member lacks colon";
                return false;
            }
            Value value;
            if (!parse_value(value, error, depth)) return false;
            if (!output.object.emplace(std::move(key), std::move(value)).second) {
                error = "duplicate object member";
                return false;
            }
            skip_space();
            if (consume('}')) return true;
            if (!consume(',')) {
                error = "object member lacks comma";
                return false;
            }
            skip_space();
            if ((_position >= _input.size()) || (_input[_position] != '"')) {
                error = "object key must be a string";
                return false;
            }
        }
    }

    bool parse_array(Value &output, std::string &error, std::size_t depth)
    {
        output = Value{};
        output.type = Value::Type::array;
        ++_position;
        skip_space();
        if (consume(']')) return true;
        while (true) {
            Value value;
            if (!parse_value(value, error, depth)) return false;
            output.array.emplace_back(std::move(value));
            skip_space();
            if (consume(']')) return true;
            if (!consume(',')) {
                error = "array member lacks comma";
                return false;
            }
        }
    }

    bool parse_string(std::string &output, std::string &error)
    {
        if (!consume('"')) {
            error = "string must begin with quote";
            return false;
        }
        output.clear();
        while (_position < _input.size()) {
            auto const character = _input[_position++];
            if (character == '"') return true;
            if (static_cast<unsigned char>(character) < 0x20) {
                error = "control character in JSON string";
                return false;
            }
            if (character != '\\') {
                output += character;
                continue;
            }
            if (_position >= _input.size()) {
                error = "unfinished JSON string escape";
                return false;
            }
            auto const escaped = _input[_position++];
            switch (escaped) {
                case '"': output += '"'; break;
                case '\\': output += '\\'; break;
                case '/': output += '/'; break;
                case 'b': output += '\b'; break;
                case 'f': output += '\f'; break;
                case 'n': output += '\n'; break;
                case 'r': output += '\r'; break;
                case 't': output += '\t'; break;
                case 'u':
                    // The typed bridge identifiers and SVG attributes are UTF-8
                    // on the wire; reject alternate UTF-16 spelling instead of
                    // risking incomplete surrogate handling in a tiny parser.
                    error = "JSON unicode escapes are not supported by bridge protocol";
                    return false;
                default:
                    error = "invalid JSON string escape";
                    return false;
            }
        }
        error = "unterminated JSON string";
        return false;
    }

    bool parse_number(Value &output, std::string &error)
    {
        auto const begin = _position;
        if (consume('-') && (_position >= _input.size())) {
            error = "invalid JSON number";
            return false;
        }
        if (consume('0')) {
            // Leading zero is valid only when it is the whole integer part.
        } else if ((_position < _input.size()) && std::isdigit(static_cast<unsigned char>(_input[_position]))) {
            while ((_position < _input.size()) && std::isdigit(static_cast<unsigned char>(_input[_position]))) ++_position;
        } else {
            error = "invalid JSON value";
            return false;
        }
        if (consume('.')) {
            auto const decimal_start = _position;
            while ((_position < _input.size()) && std::isdigit(static_cast<unsigned char>(_input[_position]))) ++_position;
            if (_position == decimal_start) {
                error = "JSON fraction lacks digits";
                return false;
            }
        }
        if ((_position < _input.size()) && ((_input[_position] == 'e') || (_input[_position] == 'E'))) {
            ++_position;
            if ((_position < _input.size()) && ((_input[_position] == '+') || (_input[_position] == '-'))) ++_position;
            auto const exponent_start = _position;
            while ((_position < _input.size()) && std::isdigit(static_cast<unsigned char>(_input[_position]))) ++_position;
            if (_position == exponent_start) {
                error = "JSON exponent lacks digits";
                return false;
            }
        }
        auto const text = _input.substr(begin, _position - begin);
        char *end = nullptr;
        auto const value = std::strtod(text.c_str(), &end);
        if ((end == nullptr) || (*end != '\0') || !std::isfinite(value)) {
            error = "JSON number is not finite";
            return false;
        }
        output = Value{};
        output.type = Value::Type::number;
        output.number = value;
        return true;
    }

    bool parse_literal(Value &output, char const *literal, Value::Type type, bool boolean, std::string &error)
    {
        auto const length = std::char_traits<char>::length(literal);
        if (_input.compare(_position, length, literal) != 0) {
            error = "invalid JSON literal";
            return false;
        }
        _position += length;
        output = Value{};
        output.type = type;
        output.boolean = boolean;
        return true;
    }

    bool parse_null(Value &output, std::string &error)
    {
        if (_input.compare(_position, 4, "null") != 0) {
            error = "invalid JSON literal";
            return false;
        }
        _position += 4;
        output = Value{};
        return true;
    }

    bool consume(char expected)
    {
        if ((_position < _input.size()) && (_input[_position] == expected)) {
            ++_position;
            return true;
        }
        return false;
    }

    void skip_space()
    {
        while ((_position < _input.size()) && std::isspace(static_cast<unsigned char>(_input[_position]))) ++_position;
    }

    std::string const &_input;
    std::size_t _position = 0;
};

} // namespace mcpinkscape::json

#endif
