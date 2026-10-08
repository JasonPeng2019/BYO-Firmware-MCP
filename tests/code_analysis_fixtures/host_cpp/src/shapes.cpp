#include "shapes.hpp"

namespace shapes {
int area(int side) { return side * side; }
double area(double radius) { return 3.0 * radius * radius; }
}

int run_shapes()
{
    double round_area = shapes::area(2.0);
    int square_area = shapes::area(3);
    return shapes::doubled(square_area) + static_cast<int>(round_area);
}
