#pragma once
namespace shapes {
int area(int side);
double area(double radius);
template <typename T> T doubled(T value) { return value + value; }
}
