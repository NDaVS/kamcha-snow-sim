"""Sample the Copernicus WGS84 PixelIsPoint DSM without changing road geometry."""
from hashlib import file_digest
from math import asin, ceil, cos, floor, radians, sin, sqrt

from PIL import Image

DEM_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
           "Copernicus_DSM_COG_10_N53_00_E158_00_DEM/"
           "Copernicus_DSM_COG_10_N53_00_E158_00_DEM.tif")


def distance(a, b):
    lon1, lat1 = map(radians, a)
    lon2, lat2 = map(radians, b)
    h = sin((lat2 - lat1) / 2)**2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2)**2
    return 12_742_017.6 * asin(sqrt(min(1, h)))


class Surface:
    def __init__(self, path):
        with path.open("rb") as file:
            self.sha256 = file_digest(file, "sha256").hexdigest()
        self.image = Image.open(path)
        keys = self.image.tag_v2[34735]
        crs = {k: value for k, location, count, value in zip(*[iter(keys[4:])]*4)
               if location == 0 and count == 1}
        if crs.get(2048) != 4326 or crs.get(1025) != 2:
            raise ValueError("expected a WGS84 PixelIsPoint Copernicus tile")
        i, j, _, lon, lat, _ = self.image.tag_v2[33922]
        self.dx, self.dy, _ = self.image.tag_v2[33550]
        self.west, self.north = lon - i * self.dx, lat + j * self.dy
        self.nodata = float(self.image.tag_v2.get(42113, -32767))
        self.pixels = self.image.load()

    def sample(self, point):
        x = (point[0] - self.west) / self.dx
        y = (self.north - point[1]) / self.dy
        i, j = floor(x), floor(y)
        if not (0 <= i < self.image.width - 1 and 0 <= j < self.image.height - 1):
            return None
        a, b, c, d = (self.pixels[i, j], self.pixels[i+1, j],
                      self.pixels[i, j+1], self.pixels[i+1, j+1])
        if any(v == self.nodata or not -500 < v < 9000 for v in (a, b, c, d)):
            return None
        u, v = x-i, y-j
        return (a*(1-u) + b*u)*(1-v) + (c*(1-u) + d*u)*v

    def profile(self, start, end, structure):
        length = distance(start, end)
        intervals = max(1, ceil(length / 10))
        values = [self.sample(tuple(a+(b-a)*part/intervals for a, b in zip(start, end)))
                  for part in range(intervals+1)]
        sampled = all(z is not None for z in values)
        result = dict(dsm_start_m=values[0], dsm_end_m=values[-1], delta_m=None,
                      mean_grade_pct=None, max_up_pct=None, max_down_pct=None,
                      ascent_m=None, descent_m=None, grade_sensitivity_low_pct=None,
                      grade_sensitivity_high_pct=None, source_id="glo30",
                      quality="structure_requires_survey" if structure else "missing_dsm",
                      length_unrounded_m=round(length, 3), profile_window_m=None)
        if structure or not sampled or length == 0:
            return result
        step = length / intervals
        delta = values[-1] - values[0]
        differences = [b-a for a, b in zip(values, values[1:])]
        span = min(intervals, ceil(40 / step))
        grades = [(b-a)*100/(step*span) for a, b in zip(values, values[span:])]
        # Endpoint perturbation of ±4 m each, for sensitivity only.
        grade, sensitivity = delta*100/length, 800/length
        return {**result, "delta_m": round(delta, 3), "mean_grade_pct": round(grade, 3),
                "max_up_pct": round(max(0, *grades), 3), "max_down_pct": round(min(0, *grades), 3),
                "ascent_m": round(step*sum(d > 0 for d in differences), 3),
                "descent_m": round(step*sum(d < 0 for d in differences), 3),
                "grade_sensitivity_low_pct": round(grade-sensitivity, 3),
                "grade_sensitivity_high_pct": round(grade+sensitivity, 3),
                "profile_window_m": round(step*span, 3),
                "quality": "shorter_than_grid" if length < 30 else "dsm_proxy"}
