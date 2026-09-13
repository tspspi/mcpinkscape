#include "bridge_json.h"

#include <cassert>
#include <string>

int main()
{
    std::string error;
    auto parsed = mcpinkscape::json::Parser(
        "{\"type\":\"request\",\"id\":\"abc\",\"params\":{\"value\":1.5,\"items\":[true,null]}}"
    ).parse(error);
    assert(parsed);
    assert(parsed->get("id")->string == "abc");
    assert(parsed->get("params")->get("items")->array.size() == 2);
    assert(!mcpinkscape::json::Parser("{\"x\":1,\"x\":2}").parse(error));
    assert(!mcpinkscape::json::Parser("[1e]").parse(error));
    return 0;
}
