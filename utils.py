import torch
import json
import torchvision
from torch.utils.data import DataLoader
from random import choice, randrange, seed
import numpy as np
import PIL
import math
from PIL import Image
import requests
from io import BytesIO
import webdataset
from webdataset.handlers import warn_and_continue
import concurrent.futures

CONNECTIONS = 16
TIMEOUT = 5
TARGET_SIZE = 128

seed(12345)

def resize_image(img):
    width, height = img.size   # Get dimensions

    rz_w = width
    rz_h = height
    _m = max(width, height)
    if _m == width:
        rz_w = math.floor((rz_w / rz_h) * TARGET_SIZE)
        rz_h = TARGET_SIZE
    if _m == height:
        rz_h = math.floor((rz_h / rz_w) * TARGET_SIZE)
        rz_w = TARGET_SIZE

    if rz_w < TARGET_SIZE:
        rz_w = TARGET_SIZE
    if rz_h < TARGET_SIZE:
        rz_h = TARGET_SIZE
    
    img = img.resize((rz_w, rz_h), resample=PIL.Image.LANCZOS)

    return img


def crop_random(img):
    img_size = img.size
    x_max = img_size[0] - TARGET_SIZE
    y_max = img_size[1] - TARGET_SIZE

    random_x = randrange(0, x_max//2 + 1) * 2
    random_y = randrange(0, y_max//2 + 1) * 2

    area = (random_x, random_y, random_x + TARGET_SIZE, random_y + TARGET_SIZE)
    c_img = img.crop(area)
    return c_img


def encode(vq, x):
    # print('econde', x.size())
    return vq.model.encode((2 * x - 1))[-1][-1]


def decode(vq, z):
    # print('decode', z.size())
    return vq.decode(z.view(z.shape[0], -1))


def log(t, eps=1e-20):
    return torch.log(t + eps)


def gumbel_noise(t):
    noise = torch.zeros_like(t).uniform_(0, 1)
    return -log(-log(noise))


def gumbel_sample(t, temperature=1., dim=-1):
    return ((t / max(temperature, 1e-10)) + gumbel_noise(t)).argmax(dim=dim)

def sample(
    model,
    c,
    x=None,
    mask=None,
    T=12,
    size=(TARGET_SIZE // 8, TARGET_SIZE // 8),
    starting_t=0,
    temp_range=[1.0, 1.0],
    typical_filtering=True,
    typical_mass=0.2,
    typical_min_tokens=1,
    classifier_free_scale=-1,
    renoise_steps=11,
    renoise_mode='start',
    c_full=None,
):
    with torch.inference_mode():
        r_range = torch.linspace(0, 1, T+1)[:-1][:, None].expand(-1, c.size(0)).to(c.device)
        temperatures = torch.linspace(temp_range[0], temp_range[1], T)
        if x is None:
            x = torch.randint(0, model.num_labels, size=(c.size(0), *size), device=c.device)
        elif mask is not None:
            noise = torch.randint(0, model.num_labels, size=(c.size(0), *size), device=c.device)
            x = noise * mask + (1-mask) * x
        init_x = x.clone()
        for i in range(starting_t, T):
            if renoise_mode == 'prev':
                prev_x = x.clone()
            r, temp = r_range[i], temperatures[i]
            # print(x.size(), c.size(), r.size(), c_full.size())
            logits = model(x, c, r, c_full)
            if classifier_free_scale >= 0:
                logits_uncond = model(x, torch.zeros_like(c), r, torch.zeros_like(c_full))
                logits = torch.lerp(logits_uncond, logits, classifier_free_scale)
            x = logits
            x_flat = x.permute(0, 2, 3, 1).reshape(-1, x.size(1))
            if typical_filtering:
                x_flat_norm = torch.nn.functional.log_softmax(x_flat, dim=-1)
                x_flat_norm_p = torch.exp(x_flat_norm)
                entropy = -(x_flat_norm * x_flat_norm_p).nansum(-1, keepdim=True)

                c_flat_shifted = torch.abs((-x_flat_norm) - entropy)
                c_flat_sorted, x_flat_indices = torch.sort(c_flat_shifted, descending=False)
                x_flat_cumsum = x_flat.gather(-1, x_flat_indices).softmax(dim=-1).cumsum(dim=-1)

                last_ind = (x_flat_cumsum < typical_mass).sum(dim=-1)
                sorted_indices_to_remove = c_flat_sorted > c_flat_sorted.gather(1, last_ind.view(-1, 1))
                if typical_min_tokens > 1:
                    sorted_indices_to_remove[..., :typical_min_tokens] = 0
                indices_to_remove = sorted_indices_to_remove.scatter(1, x_flat_indices, sorted_indices_to_remove)
                x_flat = x_flat.masked_fill(indices_to_remove, -float("Inf"))
            # x_flat = torch.multinomial(x_flat.div(temp).softmax(-1), num_samples=1)[:, 0]
            x_flat = gumbel_sample(x_flat, temperature=temp)
            x = x_flat.view(x.size(0), *x.shape[2:])
            if mask is not None:
                x = x * mask + (1-mask) * init_x
            if i < renoise_steps:
                if renoise_mode == 'start':
                    x, _ = model.add_noise(x, r_range[i+1], random_x=init_x)
                elif renoise_mode == 'prev':
                    x, _ = model.add_noise(x, r_range[i+1], random_x=prev_x)
                else:  # 'rand'
                    x, _ = model.add_noise(x, r_range[i+1])
    return x.detach()


class ProcessData:
    def __init__(self, image_size=TARGET_SIZE):
        self.transforms = torchvision.transforms.Compose([
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize(image_size),
            torchvision.transforms.RandomCrop(image_size),
        ])

    def __call__(self, data):
        data["jpg"] = self.transforms(data["jpg"])
        return data


def preprocess(image):
    w, h = image.size
    w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32
    image = image.convert('RGB')
    image = image.resize((w, h), resample=PIL.Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    # print('image', image.size())
    return image


def collate_oldbookillustrations_2(batch):
    images = torch.cat([preprocess(crop_random(resize_image(i['1600px']))) for i in batch], 0)
    captions = [i['image_alt'] if i.get('image_alt', None) is not None else
        i.get('image_caption', '') for i in batch]
    return [images, captions]


def collate_laion_coco(
    batch,
    caption_key="TEXT",
    caption_keys=["top_caption", "all_captions"],
):
    images_pil = []
    failure_idxs = []

    def load_url(url_tup, timeout):
        # print('url tup', url_tup)
        url = url_tup[1]
        response = requests.get(url, timeout=timeout)
        img = Image.open(BytesIO(response.content))
        return (img, url_tup[0])

    # Just skip any URLs we fail to download.
    url_tups = [(idx, row['URL']) for idx, row in enumerate(batch)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONNECTIONS) as executor:
        future_to_url = (executor.submit(load_url, url_t, TIMEOUT) for url_t in url_tups)
        for future in concurrent.futures.as_completed(future_to_url):
            try:
                img_tup = future.result()
                images_pil.append(img_tup)
            except Exception as e:
                # import traceback
                # traceback.print_exc()
                # print(f'Failed to get image at URL \'{urls[itr]}\'')
                pass

    images_pil = list(filter(lambda x: x is not None, images_pil))
    final_batch = []
    success_idxs = {val[1] for val in images_pil}
    failure_idxs = set(range(len(batch))) - success_idxs
    for idx, row in enumerate(batch):
        if idx in failure_idxs:
            continue
        if idx in success_idxs:
            img = next((x[0] for x in images_pil if x[1] == idx), None)
            if img is None:
                continue
            final_b_item = { 'img': img, **row }
            captions_choices = list(set(
                [final_b_item[caption_key]] +
                [final_b_item[caption_keys[0]]] +
                final_b_item[caption_keys[1]]
            ))
            final_b_item['chosen_caption'] = choice(captions_choices)
            final_batch.append(final_b_item)

    captions = [ i['chosen_caption'] for i in final_batch ]

    images = torch.cat([preprocess(crop_random(resize_image(i['img'])))
        for i in final_batch], 0)
    return [images, captions]


class ProcessDataLaionCoco:
    def __init__(self):
        self.transforms = lambda img: preprocess(crop_random(resize_image(img)))

    def __call__(self, item,
        image_key="jpg",
        caption_key="txt",
        caption_keys=["top_caption", "all_captions"],
    ):
        output = {}

        image_data = item[image_key]

        output["image_filename"] = item["__key__"]
        image_data = item[image_key]
        image = Image.open(BytesIO(image_data))
        output["jpg"] = self.transforms(image)

        # list of txt + top_caption + all_captions
        captions = [item[caption_key]] + [item[caption_keys[1]]] + \
            item[caption_keys[2]]
        text = choice(captions)

        # Do we need this?? Why is text in bytes? Does all_captions need to be
        # decoded and json parsed first?
        caption = text.decode("utf-8")
        output["txt"] = caption # text 

        metadata_file = item["json"]
        metadata = metadata_file.decode("utf-8")
        output["metadata"] = metadata

        return output


class ProcessDataLaionA:
    def __init__(self):
        self.transforms = lambda img: preprocess(crop_random(resize_image(img)))

    def __call__(self, item,
        image_key="jpg",
        caption_key="txt",
    ):
        output = {}

        image_data = item[image_key]

        output["image_filename"] = item.get("__key__")
        image_data = item[image_key]
        image = Image.open(BytesIO(image_data))
        output["jpg"] = self.transforms(image)

        text = item[caption_key]
        caption = text.decode("utf-8")
        output["txt"] = caption

        metadata_file = item["json"]
        metadata = metadata_file.decode("utf-8")
        metadata = json.loads(metadata)
        output["metadata"] = metadata

        return [output]


def collate_laion_a(batch):
    batch = list(filter(filter_laion_a_dataset, batch))
    img_tensors = [i['jpg'] for i in batch]
    images = torch.cat(img_tensors, dim=0)
    return [images, [i['txt'] for i in batch]]


def filter_laion_coco_dataset(
    item,
    image_key="URL",
    caption_key="TEXT",
    caption_keys=["top_caption", "all_captions"],
    punsafe_key='punsafe',
    height_key='HEIGHT',
    width_key='WIDTH',
):
    if height_key not in item:
        return False
    if item[height_key] is not None and item[height_key] < TARGET_SIZE:
        return False
    if width_key not in item:
        return False
    if item[width_key] is not None and item[width_key] < TARGET_SIZE:
        return False
    if punsafe_key not in item:
        return False
    if item[punsafe_key] is not None and item[punsafe_key] > 0.99:
        return False
    if caption_key not in item or item[caption_key] is None:
        return False
    if image_key not in item or item[image_key] is None:
        return False
    for c_k in caption_keys:
        if c_k not in item.keys() or item[c_k] is None:
            return False

    return True


def filter_laion_a_dataset(item,
    punsafe_key='punsafe',
    height_key='height',
    width_key='width',
):
    if "metadata" not in item:
        return False
    metadata = item['metadata']

    if height_key not in metadata:
        return False
    if metadata[height_key] < TARGET_SIZE:
        return False
    if width_key not in metadata:
        return False
    if metadata[width_key] < TARGET_SIZE:
        return False
    if punsafe_key not in metadata:
        return False
    if metadata[punsafe_key] > 0.99:
        return False

    return True


def get_dataloader(args):
    # for gigant/oldbookillustrations_2
    #
    # import datasets
    # dataset = datasets.load_dataset(args.dataset_path, split="train")
    # dataloader = DataLoader(dataset, batch_size=args.batch_size,
    #     num_workers=args.num_workers,
    #     collate_fn=collate_oldbookillustrations_2)

    # For laion-coco
    # import datasets
    # dataset = datasets.load_dataset(args.dataset_path, split="train",
    #     streaming=True)
    # torch_iterable_dataset = dataset.with_format("torch")
    # dataloader = DataLoader(
    #     torch_iterable_dataset,
    #     batch_size=args.batch_size,
    #     num_workers=args.num_workers,
    #     collate_fn=collate_laion_coco)

    # for laion/laion-a
    dataset = webdataset.WebDataset(
        args.dataset_path,
        resampled=True,
        handler=warn_and_continue,
    ) \
        .map(
            ProcessDataLaionA(),
            handler=warn_and_continue,
        )

    dataloader = DataLoader(
        dataset.batched(args.batch_size),
        batch_size=None,
        num_workers=args.num_workers,
        collate_fn=collate_laion_a,
    )

    return dataloader


def get_dataloader_laion_coco(args):
    import datasets
    dataset = datasets.load_dataset(args.dataset_path, split="train",
        streaming=True).filter(filter_laion_coco_dataset)
    torch_iterable_dataset = dataset.with_format("torch")
    dataloader = DataLoader(
        torch_iterable_dataset,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        collate_fn=collate_laion_coco)

    return dataloader
