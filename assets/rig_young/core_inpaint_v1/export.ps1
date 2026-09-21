Add-Type -AssemblyName System.Drawing
$ErrorActionPreference = 'Stop'
$dest = $PSScriptRoot
$sourceDir = 'C:/Users/lenovo/.codex/generated_images/01a0c56e-eb41-7161-9b6b-0c2791978eaa'
$layers = @(
    @{Name='face_base'; Source='exec-ac83ba4b-82fd-4db9-8812-817a9ed59d57.png'; Sx=0.69; Sy=0.69; X=200; Y=37},
    @{Name='skirt'; Source='exec-82d4dfcc-eee5-4e23-a8fb-a31042deca91.png'; Sx=0.679; Sy=0.603; X=188; Y=442},
    @{Name='torso'; Source='exec-1d32b070-9525-4974-a60d-9d16ba80fc73.png'; Sx=0.59; Sy=0.57; X=265; Y=289}
)
$results = @()
foreach ($layer in $layers) {
    $src = [System.Drawing.Bitmap]::FromFile((Join-Path $sourceDir $layer.Source))
    $bmp = [System.Drawing.Bitmap]::new(1280,1284,[System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $rect = [System.Drawing.RectangleF]::new($layer.X,$layer.Y,($src.Width*$layer.Sx),($src.Height*$layer.Sy))
    $g.DrawImage($src,$rect,[System.Drawing.RectangleF]::new(0,0,$src.Width,$src.Height),[System.Drawing.GraphicsUnit]::Pixel)
    $g.Dispose()
    $out = Join-Path $dest ($layer.Name+'.png')
    $bmp.Save($out,[System.Drawing.Imaging.ImageFormat]::Png)
    $results += [pscustomobject]@{name=$layer.Name; width=$bmp.Width; height=$bmp.Height; pixel_format=$bmp.PixelFormat.ToString(); corner_alpha=$bmp.GetPixel(0,0).A; source=$layer.Source; scale_x=$layer.Sx; scale_y=$layer.Sy; offset_x=$layer.X; offset_y=$layer.Y; registration='Approximate visual landmark registration; not pixel-exact.'}
    $bmp.Dispose()
    $src.Dispose()
}
$results | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $dest 'validation.json') -Encoding utf8
$preview = [System.Drawing.Bitmap]::new(1280,1284,[System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$pg = [System.Drawing.Graphics]::FromImage($preview)
$pg.Clear([System.Drawing.Color]::FromArgb(255,220,224,233))
foreach($part in @('skirt','face_base','torso')) {
    $im = [System.Drawing.Bitmap]::FromFile((Join-Path $dest ($part+'.png')))
    $pg.DrawImageUnscaled($im,0,0)
    $im.Dispose()
}
$pg.Dispose()
$preview.Save((Join-Path $dest 'layers_preview.png'),[System.Drawing.Imaging.ImageFormat]::Png)
$preview.Dispose()
$results | ConvertTo-Json -Depth 5
