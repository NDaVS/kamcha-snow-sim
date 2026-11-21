"""Draw 1080p frames and compile H.264 using the same atlas projection."""
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import math
import os
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont
from shapely.geometry import LineString, box

from .heatmap_collect import OUT
from .heatmap_data import FIELDS, local_time
from .heatmap_maps import geometry_parts, interpolate

W,H=1920,1080
FPS=6
WIND_FIELDS={"wind_speed","wind_gust","wind_deg"}
MAP=(48,146,1300,998)
BG="#0c151e"
PANEL="#10202b"
WATER="#102c3b"
LAND="#172833"
MASK="#263945"
INK="#eaf2f4"
MUTED="#9db2bf"
EDGE="#43616e"
ROAD="#ad9b77"
ACCENT="#91ddd7"


@lru_cache
def font(size,bold=False):
    name="Arial Bold.ttf" if bold else "Arial.ttf"
    linux="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    candidates=[Path("/System/Library/Fonts/Supplemental")/name,Path(linux)]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(path,size)
    return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",size)


def text(draw,pos,value,size=22,color=INK,bold=False,anchor="lt"):
    draw.text(pos,str(value),fill=color,font=font(size,bold),anchor=anchor)


def lut(colors):
    rgb=np.array([ImageColor.getrgb(c) for c in colors])
    times=np.linspace(0,255,len(colors))
    return np.column_stack([np.round(np.interp(np.arange(256),times,rgb[:,i]))
                            for i in range(3)]).astype(np.uint8)


def color_scale(values, spec, at_least):
    a,b=spec[2]
    minimum=float(values[at_least].min()) if np.any(at_least) else a
    maximum=float(values[at_least].max()) if np.any(at_least) else b
    if minimum<a:
        a=math.floor(minimum)
    if maximum>b:
        b=math.ceil(maximum)
    return (a,b)


def scaled_colors(values, lower, upper, rgb):
    flags=np.isfinite(values)
    t=(np.where(flags,values,lower)-lower)/(upper-lower)
    index=np.round(255*np.clip(t,0,1)).astype(np.uint8)
    return rgb[index],flags


def show_num(value,precision=1):
    return f"{value:.{precision}f}".rstrip("0").rstrip(".").replace("-","−").replace(".",",") if precision else str(round(value))


class FrameRenderer:
    def __init__(self,model,source):
        self.model=model
        self.source=source
        self.wind_order=sorted(range(len(model["points"])),
                               key=lambda i:model["points"][i]["label_offset"] is None)
        self.history=source.startswith("Open-Meteo")
        left,top,right,bottom=MAP
        self.factor=min((right-left-140)/model["nx"],(bottom-top-32)/model["ny"])
        self.base_x=round((right+left-model["nx"]*self.factor)/2)
        self.base_y=round((bottom+top-model["ny"]*self.factor)/2)
        self.sw=round(model["nx"]*self.factor)
        self.sh=round(model["ny"]*self.factor)
        self.base=Image.new("RGB",(W,H),BG)
        draw=ImageDraw.Draw(self.base)
        draw.rounded_rectangle(MAP,20,fill=LAND,outline="#304856",width=2)
        area=Image.new("L",(W,H))
        painter=ImageDraw.Draw(area)
        for polygon in model["layers"]["water"]:
            self.paint_polygon(painter,polygon,240,0)
        area=area.crop((left+2,top+2,right-2,bottom-2))
        self.base.paste(WATER,(left+2,top+2,right-2,bottom-2),area)
        draw=ImageDraw.Draw(self.base)
        self.city(draw)
        self.clip=Image.new("L",(self.sw,self.sh))
        painter=ImageDraw.Draw(self.clip)
        for polygon in model["layers"]["footprint"]:
            self.paint_polygon(painter,polygon,255,0,relative=True)
        self.base.paste(MASK,(self.base_x,self.base_y,self.base_x+self.sw,self.base_y+self.sh),self.clip)
        self.clip_array=np.asarray(self.clip,dtype=np.uint8)
        self.label_entries=[(p,self.screen(*p["xy"])) for p in model["points"] if p["label_offset"]]
        text(draw,(48,29),"Петропавловск-Камчатский",23,ACCENT,True)
        draw.line((48,119,1870,119),fill="#243b48",width=2)
        text(draw,(48,1032),f"{source}  •  © OpenStreetMap contributors",19,MUTED)
        text(draw,(1872,1032),"UTM 57N  /  Камчатка UTC+12",19,MUTED,anchor="rt")
        draw.rounded_rectangle((1330,146,1871,998),20,fill=PANEL,outline="#304856",width=2)
        self.map_widgets(draw)

    def screen(self,x,y,relative=False):
        return (round(x*self.factor+(0 if relative else self.base_x)),
                round(y*self.factor+(0 if relative else self.base_y)))

    def paint_polygon(self,draw,polygon,fill,hole,relative=False):
        if not polygon:
            return
        draw.polygon([self.screen(*xy,relative=relative) for xy in polygon[0]],fill=fill)
        for ring in polygon[1:]:
            draw.polygon([self.screen(*xy,relative=relative) for xy in ring],fill=hole)

    def city(self,draw):
        bounds=box(MAP[0]+3,MAP[1]+3,MAP[2]-3,MAP[3]-3)
        for polygon in self.model["layers"]["city"]:
            for ring in polygon:
                line=LineString([self.screen(*p) for p in ring]).intersection(bounds)
                for part in geometry_parts(line):
                    draw.line(list(part[0]),fill=EDGE,width=2)

    def map_widgets(self,draw):
        x,y=109,910
        distance=round(self.factor*2000/self.model["step"])
        draw.rounded_rectangle((78,862,145+distance,944),12,fill="#10202b",outline="#2b4655")
        draw.line((x,y,x+distance,y),fill=INK,width=3)
        for dx in (0,distance//2,distance):
            draw.line((x+dx,y-6,x+dx,y+6),fill=INK,width=2)
        text(draw,(x,y-34),"2 км",19,MUTED)
        text(draw,(96,181),"С",20,INK,True,anchor="mt")
        draw.line((96,217,96,244),fill=INK,width=2)
        draw.polygon([(96,211),(90,223),(102,223)],fill=INK)
        text(draw,(300,697),"Авачинская губа",21,"#6e9aaf",anchor="mt")

    def outline_roads(self, draw):
        for path in self.model["layers"]["arterials"]:
            for segment in path:
                draw.line([self.screen(*p) for p in segment],fill="#102430",width=5)
                draw.line([self.screen(*p) for p in segment],fill=ROAD,width=2)

    def labels(self,draw):
        for p,(x,y) in self.label_entries:
            dx,dy=p["label_offset"]
            dx=dx*1.36
            dy=dy*1.36
            label=p["district"]
            if label is None:
                continue
            end=(x+dx,y+dy)
            draw.line((x,y,*end),fill="#8fa2af",width=1)
            draw.text(end,label,fill=INK,stroke_fill="#0b151e",stroke_width=3,
                      font=font(17,True),anchor="lm" if dx>0 else "rm")

    def wind_arrows(self,draw,headings,speeds):
        occupied=set()
        for index in self.wind_order:
            heading,speed=headings[index],speeds[index]
            if not np.isfinite(heading) or (np.isfinite(speed) and speed<0.3):
                continue
            x,y=self.screen(*self.model["points"][index]["xy"])
            cell=x//48,y//48
            if cell in occupied:
                continue
            occupied.add(cell)
            angle=math.radians(float(heading))
            dx,dy=math.sin(angle),-math.cos(angle)
            start=(x+dx*5,y+dy*5)
            tip=(x+dx*23,y+dy*23)
            edge1=(tip[0]-dx*7+dy*5,tip[1]-dy*7-dx*5)
            edge2=(tip[0]-dx*7-dy*5,tip[1]-dy*7+dx*5)
            for color,width in (("#0c1823",5),(INK,2)):
                draw.line([start,tip],fill=color,width=width)
                draw.line([edge1,tip,edge2],fill=color,width=width)

    def draw_frame(self, field, spec, extent, values, epoch, index, total, out, wind):
        label,units,_,palette=spec
        a,b=extent
        rgb=lut(palette)
        grid=interpolate(self.model,values,circular=field=="wind_deg")
        clr,valid=scaled_colors(grid,a,b,rgb)
        pixels=Image.fromarray(np.dstack([clr,np.where(valid,211,0).astype(np.uint8)]),"RGBA")
        resized=pixels.resize((self.sw,self.sh),resample=Image.Resampling.NEAREST)
        alpha=np.minimum(np.asarray(resized.getchannel("A")),self.clip_array)
        pic=self.base.copy()
        pic.paste(resized.convert("RGB"),(self.base_x,self.base_y),Image.fromarray(alpha))
        draw=ImageDraw.Draw(pic)
        self.outline_roads(draw)
        dot_colors,known=scaled_colors(values,a,b,rgb)
        for i,point in enumerate(self.model["points"]):
            x,y=self.screen(*point["xy"])
            r=4 if known[i] else 2
            clr=tuple(int(v) for v in dot_colors[i]) if known[i] else "#738693"
            draw.ellipse((x-r,y-r,x+r,y+r),outline="#0c151e",width=1,fill=clr)
        if wind is not None:
            self.wind_arrows(draw,*wind)
        self.labels(draw)
        text(draw,(48,61),label,47,INK,True)
        text(draw,(1870,53),local_time(epoch).strftime("%d.%m.%Y   %H:%M"),42,INK,True,anchor="rt")
        x=1374
        period="ЧАС АРХИВА" if self.history else "СРОК ПРОГНОЗА"
        text(draw,(x,184),f"{period}   {index+1:02d} / {total:02d}",18,ACCENT,True)
        text(draw,(x,226),local_time(epoch).strftime("%d.%m  ·  %H:%M"),61,INK,True)
        text(draw,(x,315),"Число точек с данными",21,MUTED)
        text(draw,(x,350),f"{int(known.sum())} / {len(values)}",46,INK,True)
        if known.any():
            low,high=np.min(values[known]),np.max(values[known])
            title=f"{show_num(low,2)} … {show_num(high,2)} {units}"
        else:
            title="Данных в этом сроке нет"
        text(draw,(x,429),"Диапазон по точкам",21,MUTED)
        text(draw,(x,465),title,34,INK,True)
        text(draw,(x,550),f"Шкала, {units}",22,INK)
        ramp=np.repeat(rgb[np.newaxis,:,:],24,axis=0)
        pic.paste(Image.fromarray(ramp).resize((448,23)),(x,587))
        for i in range(5):
            frac=i/4
            mark=show_num(a+(b-a)*frac,1)
            if self.history and field=="visibility" and i==4:
                mark="≥ "+mark
            text(draw,(x+frac*448,622),mark,18,MUTED,
                 anchor=("lt" if i==0 else "rt" if i==4 else "mt"))
        draw.line((x,666,1821,666),fill="#2e4958",width=1)
        for y,clr,caption in [(709,ACCENT,"Значение API в точке"),
                               (750,MASK,"Зона без опоры для интерполяции"),
                               (791,ROAD,"Основные дороги OSM")]:
            draw.ellipse((x,y,x+11,y+11),fill=clr)
            text(draw,(x+24,y-4),caption,19,MUTED)
        text(draw,(x,853),"Пространственная оценка IDW",22,INK,True)
        text(draw,(x,891),"До 1 км от опоры, по рабочей маске OSM",18,MUTED)
        if wind is not None:
            text(draw,(x,928),"Стрелка показывает, откуда дует ветер",18,MUTED)
        if not known.any():
            draw.rounded_rectangle((419,496,921,566),12,fill=PANEL,outline=EDGE,width=2)
            text(draw,(670,531),"Поле отсутствует в ответах",29,INK,True,anchor="mm")
        pic.save(out,compress_level=3)


def video_for(field, path, directory, series):
    target=directory/f"{field}_{series}_heatmap.mp4"
    command=["ffmpeg","-hide_banner","-loglevel","error","-y","-framerate",str(FPS),
             "-i",str(path/"%03d.png"),"-c:v","libx264","-preset","veryfast","-crf","18",
             "-pix_fmt","yuv420p","-movflags","+faststart",str(target)]
    subprocess.run(command,check=True)
    return target


def render_all(model, cube, extents, timestamps, *, fields=FIELDS, directory=OUT,
               series="forecast", source="OpenWeather Forecast"):
    rend=FrameRenderer(model,source)
    names=list(fields)
    headings,speeds=cube[names.index("wind_deg")],cube[names.index("wind_speed")]
    for index,(field,spec) in enumerate(fields.items()):
        path=directory/"frames"/field
        path.mkdir(parents=True,exist_ok=True)
        for frame in path.glob("[0-9][0-9][0-9].png"):
            if int(frame.stem)>=len(timestamps):
                frame.unlink()
        with ThreadPoolExecutor(max_workers=min(5,os.cpu_count() or 1)) as pool:
            work=[pool.submit(rend.draw_frame,field,spec,extents[field],cube[index,t],dt,t,len(timestamps),
                              path/f"{t:03}.png",(headings[t],speeds[t]) if field in WIND_FIELDS else None)
                  for t,dt in enumerate(timestamps)]
            for item in work:
                item.result()
        video_for(field,path,directory,series)
        print(f"{field} · {len(timestamps)} PNG + MP4",flush=True)
    names=["temp","snow","wind_speed","visibility","humidity"]
    command=["ffmpeg","-hide_banner","-loglevel","error","-y"]
    for field in names:
        command+=["-i",str(directory/f"{field}_{series}_heatmap.mp4")]
    filename="summary_weather_history.mp4" if series=="history" else "summary_weather_heatmaps.mp4"
    command+=["-filter_complex","".join(f"[{i}:v]" for i in range(len(names)))+
              f"concat=n={len(names)}:v=1:a=0[show]","-map","[show]","-c:v","libx264","-preset","veryfast",
              "-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",str(directory/filename)]
    subprocess.run(command,check=True)
